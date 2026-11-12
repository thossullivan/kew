"""Publish an agent checkout through a fresh host-owned bare repository.

The checkout supplies file bytes only. No git process resolves its work tree or
metadata. The remote and every parent come from operator-controlled inputs.
"""

from __future__ import annotations

import hashlib
import re
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from email.utils import format_datetime
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any, Literal, NoReturn

from kew.config import project_root
from kew.credential_surface import redact_credentials
from kew.progress import _warn
from kew.publication import (
    PublicationError,
    TransientPushFailure,
    publication_git_env,
    transient_push_rejection,
)
from kew.publication_artifact import MAX_BLOB_BYTES
from kew.publication_copy_sync import CopySync, copy_sync_flags
from kew.publication_extract import (
    OVERSIZE_BASE_BLOB,
    extract_in_container,
    extract_on_host,
)
from kew.publication_filter import ReviewRequired
from kew.publication_repository import (
    CanonicalRepository,
    CanonicalRepositoryError,
    _is_remote_url,
    _read_env,
    _require_host_owned,
    commit_tree,
    fetch_commit,
    fetch_history,
    promote,
    provision,
    publication_write_env,
    read_commit,
)
from kew.publication_scan import OutputScanError, OutputScanTarget, range_truncated
from kew.repo_host import repo_host
from kew.subprocess_utils import popen_bytes, run, run_bytes

if TYPE_CHECKING:
    from kew.publication_artifact import Artifact
    from kew.publication_repository import IdentityLike, PromotedTree
    from kew.run_store import RunStore

__all__ = [
    "ACCEPTANCE_TEST_CHANGED",
    "AcceptanceContract",
    "acceptance_contracts",
    "CanonicalIdentity",
    "CopySync",
    "republish_agent_head",
    "CanonicalNoChange",
    "CanonicalPublication",
    "PublicationHeld",
    "checkout_changed_paths",
    "publish_checkout",
    "publication_remote",
    "remote_commit_is_ancestor",
    "remote_branch_sha",
    "host_commit_identity",
]

#: Reading the base for digests is git plumbing over a local bare repo; a
#: process that has not finished in this long is stalled, not slow.
BASE_SCAN_TIMEOUT_SECONDS = 300


#: How many times host publication sends one commit at a remote that failed it
#: on its own side, and how long it waits between tries — three tries over about
#: a minute. Same policy the dispatch push has used since #1218; #2455 brought
#: the review publication path under it, where a single "remote: Internal Server
#: Error" used to fail a finished 97-step remediation and send the epic loop off
#: to redo the whole fix.
PUSH_ATTEMPTS = 3
PUSH_DELAYS = (20.0, 40.0)

#: How many commits deep, from the agent head, the host fetches an
#: agent-pushed range to scan it whole. Deepening stops here, or at
#: BASE_SCAN_TIMEOUT_SECONDS; a range still cut short then refuses as
#: ``credential_scan_unavailable``. The scan's byte bounds cap its volume.
AGENT_RANGE_MAX_COMMITS = 256
_AGENT_RANGE_FIRST_DEPTH = 4

_OID = re.compile(r"[0-9a-f]{40}")
_SCP_LIKE = re.compile(r"[^/@:]+@[^/@:]+:\S+")
_LONG_CONFLICT_MARKER = re.compile(r"^(?:<{7,}|\|{7,}|>{7,})(?: |$)")
_CONFLICT_OPENING = re.compile(r"^<{1,6}(?!<)( |$)")
_CONFLICT_DIVIDER = re.compile(r"^=+[ \t]*$")
_CONFLICT_CLOSING = re.compile(r"^>{1,6}(?!>)( |$)")


#: The hold reason for a publication that would not leave a node's acceptance
#: test file byte-identical to the contract the ledger recorded (#2729).
ACCEPTANCE_TEST_CHANGED = "acceptance-test-changed"


@dataclass(frozen=True)
class AcceptanceContract:
    """A node's acceptance test file as the ledger recorded it."""

    path: str
    sha256: str


def acceptance_contracts(
    store: RunStore | None, item_id: str | None
) -> tuple[AcceptanceContract, ...]:
    """The item's current acceptance contracts, which a publication must keep."""
    if store is None or not item_id:
        return ()
    return tuple(
        AcceptanceContract(path=row.path, sha256=row.sha256)
        for row in store.current_acceptance_contracts_for_item(item_id)
    )


@dataclass(frozen=True)
class CanonicalPublication:
    """The verified commit produced and pushed from the canonical repository."""

    commit_sha: str
    tree_sha: str
    parent_shas: tuple[str, ...]
    changed_paths: tuple[str, ...]
    flags: tuple[str, ...]


@dataclass(frozen=True)
class CanonicalNoChange:
    """The verified empty artifact and trusted base from canonical extraction."""

    artifact: Artifact
    base_sha: str
    tree_sha: str

    def __bool__(self) -> Literal[False]:
        """Keep legacy truth tests false while preserving explicit evidence."""
        return False


@dataclass(frozen=True)
class CanonicalIdentity:
    """The fixed identity used for host-authored canonical commits."""

    name: str
    email: str


def host_commit_identity() -> CanonicalIdentity:
    """Return identity that cannot be influenced by checkout Git config."""
    return CanonicalIdentity(name="kew", email="kew@invalid.local")


class PublicationHeld(Exception):
    """The filter requires human review. Nothing was pushed."""

    def __init__(self, flags: tuple[str, ...]) -> None:
        self.flags = flags
        listing = "; ".join(flags[:5])
        more = "" if len(flags) <= 5 else f" (and {len(flags) - 5} more)"
        super().__init__(f"publication needs human review — {listing}{more}")


@dataclass(frozen=True)
class _CommitIntent:
    tree_sha: str
    parent_sha: str | None
    message: str
    author: IdentityLike
    committer: IdentityLike
    when: str
    extra_parents: tuple[str, ...] = ()


def _require_oid(value: str, label: str) -> None:
    if not _OID.fullmatch(value):
        raise CanonicalRepositoryError(f"{label} {value!r} is not a full SHA-1 id")


def _require_branch(branch: str, *, cwd: Path) -> None:
    result = run(
        ["git", "-C", str(cwd), "check-ref-format", "--branch", branch],
        check=False,
        env=_read_env(),
    )
    if result.returncode != 0:
        raise CanonicalRepositoryError(f"branch {branch!r} is not a valid ref name")


def _operator_origin() -> str:
    root = project_root()
    result = run(
        ["git", "-C", str(root), "config", "--local", "--get", "remote.origin.url"],
        check=False,
        env=_read_env(),
    )
    remote = (result.stdout or "").strip()
    if result.returncode != 0 or not remote or "\n" in remote:
        raise PublicationError("push", "operator origin URL is unavailable")
    if _is_remote_url(remote) or _SCP_LIKE.fullmatch(remote):
        return remote
    path = Path(remote).expanduser()
    return str(path if path.is_absolute() else (root / path).resolve())


def publication_remote(repo: str | None) -> str:
    """Resolve an operator-controlled publication remote without the checkout."""
    value = (repo or "").strip()
    if value:
        if _is_remote_url(value) or _SCP_LIKE.fullmatch(value):
            return value
        path = Path(value).expanduser()
        if path.is_absolute() or path.exists():
            return str(path.resolve())
        if publication_git_env() is not None:
            return f"https://{repo_host(project_root())}/{value}.git"
    return _operator_origin()


def _require_remote(remote: str) -> None:
    if not (_is_remote_url(remote) or _SCP_LIKE.fullmatch(remote)):
        _require_host_owned(remote)


def remote_branch_sha(remote: str, branch: str, *, scratch: Path) -> str:
    """Return one exact remote branch head without resolving the checkout."""
    scratch.mkdir(parents=True, mode=0o700, exist_ok=True)
    _require_branch(branch, cwd=scratch)
    _require_remote(remote)
    reference = f"refs/heads/{branch}"
    result = run(
        ["git", "ls-remote", "--exit-code", "--", remote, reference],
        cwd=scratch,
        check=False,
        env=publication_write_env(),
    )
    if result.returncode == 2:
        return ""
    if result.returncode != 0:
        detail = redact_credentials(result.stderr or result.stdout or "").strip()[:300]
        raise PublicationError(
            "push", f"cannot query remote branch {branch!r}: {detail}"
        )
    rows = (result.stdout or "").splitlines()
    if len(rows) != 1:
        raise PublicationError(
            "push", f"remote branch query for {branch!r} was ambiguous"
        )
    sha, separator, found = rows[0].partition("\t")
    if separator != "\t" or found != reference or not _OID.fullmatch(sha):
        raise PublicationError(
            "push", f"remote branch query for {branch!r} was malformed"
        )
    return sha


def remote_commit_is_ancestor(
    remote: str,
    ancestor_sha: str,
    descendant_sha: str,
    *,
    scratch: Path,
) -> bool:
    """Prove ancestry inside a fresh host-owned canonical repository."""
    _require_oid(ancestor_sha, "ancestor")
    _require_oid(descendant_sha, "descendant")
    _require_remote(remote)
    repo = provision(scratch / "canonical", source=remote, base_sha=descendant_sha)
    fetch_history(repo, source=remote, sha=descendant_sha)
    _fetch_parents(repo, remote=remote, parents=(ancestor_sha, descendant_sha))
    result = run(
        [
            "git",
            "-C",
            str(repo.path),
            "merge-base",
            "--is-ancestor",
            ancestor_sha,
            descendant_sha,
        ],
        check=False,
        env=_read_env(),
    )
    if result.returncode in (0, 1):
        return result.returncode == 0
    detail = redact_credentials(result.stderr or result.stdout or "").strip()[:300]
    raise CanonicalRepositoryError(f"could not prove canonical ancestry: {detail}")


def _fetch_parents(
    repo: CanonicalRepository, *, remote: str, parents: tuple[str, ...]
) -> None:
    for parent in parents:
        if parent != repo.base_sha and not repo.holds_commit(parent):
            fetch_commit(repo, source=remote, sha=parent)
        if not repo.holds_commit(parent):
            raise CanonicalRepositoryError(f"parent {parent} is absent after fetch")


def _watchdog(process: Any, seconds: float) -> threading.Timer:
    """Kill *process* after *seconds* so a stalled cat-file cannot block."""
    timer = threading.Timer(seconds, process.kill)
    timer.daemon = True
    timer.start()
    return timer


def _batch_session(repo: CanonicalRepository, *args: str) -> Any:
    return popen_bytes(
        ["git", "-C", str(repo.path), "cat-file", *args],
        env=_read_env(),
    )


def _finish_batch(process: Any, timer: threading.Timer, what: str) -> None:
    process.stdin.close()
    returncode = process.wait()
    timer.cancel()
    if returncode != 0:
        detail = redact_credentials(
            process.stderr.read().decode("utf-8", errors="replace")
        )[:300]
        raise CanonicalRepositoryError(f"could not {what}: {detail}")


def _base_blob_sizes(
    repo: CanonicalRepository, objects: Mapping[str, str]
) -> dict[str, int]:
    """Size of every base blob from one bounded cat-file --batch-check."""
    sizes: dict[str, int] = {}
    process = _batch_session(repo, "--batch-check")
    timer = _watchdog(process, BASE_SCAN_TIMEOUT_SECONDS)
    try:
        for path, oid in objects.items():
            process.stdin.write(f"{oid}\n".encode())
            process.stdin.flush()
            header = process.stdout.readline().rstrip(b"\n").split()
            if len(header) != 3 or header[0] != oid.encode() or header[1] != b"blob":
                raise CanonicalRepositoryError(
                    f"batch-check response for base {path!r} is malformed"
                )
            sizes[path] = int(header[2])
        _finish_batch(process, timer, "size base blobs")
    except BaseException:
        timer.cancel()
        process.kill()
        process.wait()
        raise
    finally:
        process.stdout.close()
    return sizes


def _stream_base_digest(process: Any, *, path: str, oid: str) -> str:
    """Request one blob on the batch stream and hash it without retaining it."""
    process.stdin.write(f"{oid}\n".encode())
    process.stdin.flush()
    header = process.stdout.readline().rstrip(b"\n").split()
    if len(header) != 3 or header[0] != oid.encode() or header[1] != b"blob":
        raise CanonicalRepositoryError(f"batch response for base {path!r} is malformed")
    remaining = int(header[2])
    digest = hashlib.sha256()
    while remaining:
        chunk = process.stdout.read(min(64 * 1024, remaining))
        if not chunk:
            raise CanonicalRepositoryError(f"base blob {path!r} ended early")
        digest.update(chunk)
        remaining -= len(chunk)
    if process.stdout.read(1) != b"\n":
        raise CanonicalRepositoryError(f"base blob {path!r} has no terminator")
    return digest.hexdigest()


def _base_digests(
    repo: CanonicalRepository, objects: Mapping[str, str]
) -> dict[str, str]:
    """sha256 of every base blob the walker could match, through two bounded
    cat-file processes: one --batch-check for sizes, one --batch for content.

    Blobs above MAX_BLOB_BYTES are not requested at all (the walker refuses a
    checkout file that large before it would compare), and both processes are
    killed after BASE_SCAN_TIMEOUT_SECONDS so a stalled git cannot block a
    publication or a budget probe (review rounds 5 and 6).
    """
    digests: dict[str, str] = {}
    if not objects:
        return digests
    sizes = _base_blob_sizes(repo, objects)
    wanted: dict[str, str] = {}
    for path, oid in objects.items():
        if sizes[path] <= MAX_BLOB_BYTES:
            wanted[path] = oid
        else:
            digests[path] = OVERSIZE_BASE_BLOB
    if not wanted:
        return digests
    process = _batch_session(repo, "--batch")
    timer = _watchdog(process, BASE_SCAN_TIMEOUT_SECONDS)
    try:
        for path, oid in wanted.items():
            digests[path] = _stream_base_digest(process, path=path, oid=oid)
        _finish_batch(process, timer, "read base blobs")
    except BaseException:
        timer.cancel()
        process.kill()
        process.wait()
        raise
    finally:
        process.stdout.close()
    return digests


def _base_blob_reader(
    repo: CanonicalRepository,
) -> tuple[Mapping[str, str], Callable[[str], bytes], dict[str, str]]:
    state = repo.base_state()
    objects = repo.base_blob_oids()

    def read_base_blob(path: str) -> bytes:
        oid = objects.get(path)
        if oid is None:
            raise CanonicalRepositoryError(f"base has no blob for {path!r}")
        return repo.read_blob(oid)

    return state, read_base_blob, _base_digests(repo, objects)


def _extract(
    repo: CanonicalRepository,
    checkout_dir: Path,
    artifact_dir: Path,
    *,
    runtime: str | None,
    image: str | None,
    proxy_environment: str,
) -> Artifact:
    if (runtime is None) != (image is None):
        raise CanonicalRepositoryError("runtime and image must be supplied together")
    if runtime is not None and image is not None:
        return extract_in_container(
            runtime=runtime,
            image=image,
            checkout_dir=checkout_dir,
            artifact_dir=artifact_dir,
            slug=f"publish-{repo.base_sha[:12]}",
            base_sha=repo.base_sha,
            proxy_environment=proxy_environment,
        )
    state, reader, digests = _base_blob_reader(repo)
    return extract_on_host(
        checkout_dir,
        artifact_dir,
        state,
        reader,
        repo.base_sha,
        base_digests=digests,
    )


def _promote(
    repo: CanonicalRepository,
    artifact: Artifact,
    *,
    scratch: Path,
    merged_base_digest: Callable[[str], str | None] | None = None,
) -> PromotedTree:
    try:
        return promote(
            repo, artifact, scratch=scratch, merged_base_digest=merged_base_digest
        )
    except ReviewRequired as exc:
        flags = tuple(f"{flag.path}: {flag.reason}" for flag in exc.flags)
        raise PublicationHeld(flags) from exc


def _acceptance_flags(
    repo: CanonicalRepository,
    artifact: Artifact,
    contracts: tuple[AcceptanceContract, ...],
) -> tuple[str, ...]:
    """Name each contract file the published tree would not hold byte-identical.

    The published bytes at a path are the agent's write, nothing for a
    deletion, or else what the base already holds. A base that will not answer
    is not a match, so it holds too: the contract is only kept when its bytes
    are proven.
    """
    if not contracts:
        return ()
    written = {entry.path: entry.sha256 for entry in artifact.entries}
    deleted = set(artifact.deletions)
    base = _tree_digest_reader(repo, repo.base_sha)
    flags: list[str] = []
    for contract in contracts:
        if contract.path in deleted:
            digest = None
        elif contract.path in written:
            digest = written[contract.path]
        else:
            digest = base(contract.path)
        if digest != contract.sha256:
            flags.append(f"{contract.path}: {ACCEPTANCE_TEST_CHANGED}")
    return tuple(flags)


def _promote_under_contract(
    repo: CanonicalRepository,
    artifact: Artifact,
    *,
    scratch: Path,
    merged_base_digest: Callable[[str], str | None] | None,
    acceptance: tuple[AcceptanceContract, ...],
) -> PromotedTree:
    """Promote *artifact*, holding it when it changes an acceptance contract.

    The contract's reason leads any hold, so a changed acceptance file is named
    as such even when the filter also flags it as a protected path.
    """
    changed = _acceptance_flags(repo, artifact, acceptance)
    try:
        promoted = _promote(
            repo, artifact, scratch=scratch, merged_base_digest=merged_base_digest
        )
    except PublicationHeld as held:
        if changed:
            raise PublicationHeld((*changed, *held.flags)) from held
        raise
    if changed:
        raise PublicationHeld(changed)
    return promoted


#: What a tree answers when it could not be read at all, as distinct from
#: ``None`` — "that tree holds nothing at this path". Neither value is a
#: digest, so a comparison against one is false either way; the distinction is
#: for a rule that reads a tree as an EXEMPTION, where "it would not say" must
#: never be recorded as "it does not hold that" (#2457).
_TREE_UNREADABLE = "tree-unreadable"


class _TreeUnreadable(RuntimeError):
    """A tree a rule needed as EXEMPTING evidence could not be read."""


def _tree_blob_oids(
    repo: CanonicalRepository, commit_sha: str
) -> dict[str, str] | None:
    """Every blob oid in one commit's tree, or ``None`` when it would not list."""
    try:
        return repo.tree_blob_oids(commit_sha)
    except CanonicalRepositoryError as exc:
        _warn(f"tree {commit_sha[:12]} could not be listed: {exc}")
        return None


def _blob_digest(repo: CanonicalRepository, oid: str, *, tree: str, path: str) -> str:
    """One blob's sha256, or ``_TREE_UNREADABLE`` when it would not read."""
    try:
        return hashlib.sha256(repo.read_blob(oid)).hexdigest()
    except CanonicalRepositoryError as exc:
        _warn(f"{tree} blob for {path} could not be read: {exc}")
        return _TREE_UNREADABLE


def _tree_digest_reader(
    repo: CanonicalRepository, commit_sha: str
) -> Callable[[str], str | None]:
    """Read one path's content digest out of one commit's tree.

    Lazy and memoized: the tree is listed on the first question and a blob is
    read only for a path a rule actually asked about, which is a handful at
    most. ``None`` means "that tree holds nothing at this path" and
    ``_TREE_UNREADABLE`` means "it could not answer" — the listing failed, or
    the blob would not read. Neither equals a digest, so a rule that flags
    unless some tree matches keeps flagging for either; only a rule that
    must not read one as the other needs them apart, and :func:`_answered` is
    how it asks.
    """
    oids: dict[str, str] | None = None
    listed = False
    digests: dict[str, str | None] = {}

    def digest(path: str) -> str | None:
        nonlocal oids, listed
        if path in digests:
            return digests[path]
        if not listed:
            listed = True
            oids = _tree_blob_oids(repo, commit_sha)
        if oids is None:
            return _TREE_UNREADABLE
        oid = oids.get(path)
        value = (
            None
            if oid is None
            else _blob_digest(repo, oid, tree=commit_sha[:12], path=path)
        )
        digests[path] = value
        return value

    return digest


def _answered(
    reader: Callable[[str], str | None], *, tree: str
) -> Callable[[str], str | None]:
    """*reader*, for a rule that must tell "nothing there" from "would not say".

    Read as "nothing", an unanswered question either excuses a path or flags
    it — whichever way the rule happens to compare — on evidence nobody read.
    The failure is raised instead, so the rule can hold the publication under
    a reason that says what could not be read (#2633).
    """

    def answer(path: str) -> str | None:
        value = reader(path)
        if value == _TREE_UNREADABLE:
            raise _TreeUnreadable(f"{tree} would not answer for {path}")
        return value

    return answer


def _merged_base_digests(
    repo: CanonicalRepository, *, remote: str, merged_base_sha: str | None
) -> Callable[[str], str | None] | None:
    """The merged base's content reader, or ``None`` when there is no answer.

    BEST EFFORT, deliberately. Without it every protected path is flagged for a
    person exactly as before, so a base that cannot be fetched or read must
    never fail a publication that would otherwise succeed — it only takes away
    the exemption. A sha equal to the trusted base answers nothing new (the
    filter already compares against that tree), so it is not fetched.
    """
    if not merged_base_sha or merged_base_sha == repo.base_sha:
        return None
    try:
        if not repo.holds_commit(merged_base_sha):
            fetch_commit(repo, source=remote, sha=merged_base_sha)
    except (CanonicalRepositoryError, OSError) as exc:
        _warn(f"merged base {merged_base_sha[:12]} is unavailable: {exc}")
        return None
    if not repo.holds_commit(merged_base_sha):
        return None
    return _tree_digest_reader(repo, merged_base_sha)


def _copy_sync_readers(
    repo: CanonicalRepository,
    *,
    remote: str,
    integrated_digest: Callable[[str], str | None] | None,
    merged_base_sha: str | None,
    upstream_sha: str,
) -> tuple[
    Callable[[str], str | None],
    Callable[[str], str | None],
    Callable[[str], str | None],
]:
    """The branch, integration and base-head readers, each raising when unread.

    :func:`_merged_base_digests` answers ``None`` for two unlike facts. When
    the integrated base IS the trusted base there is only one tree to read, and
    the branch's own reader is it. When a DISTINCT integrated base — or the
    base head itself — could not be fetched or read, the rule has no evidence
    to judge with, so :class:`_TreeUnreadable` is raised and the caller holds.
    """
    base = _answered(_tree_digest_reader(repo, repo.base_sha), tree="the branch")
    integrated = base
    if integrated_digest is not None:
        integrated = _answered(integrated_digest, tree="the integrated base")
    elif merged_base_sha and merged_base_sha != repo.base_sha:
        raise _TreeUnreadable(
            f"the integrated base {merged_base_sha[:12]} could not be read"
        )
    upstream = _merged_base_digests(repo, remote=remote, merged_base_sha=upstream_sha)
    if upstream is None:
        raise _TreeUnreadable(f"the base head {upstream_sha[:12]} could not be read")
    return base, integrated, _answered(upstream, tree="the base head")


#: Why a publication the copy-sync rule could not judge is held (#2633).
_COPY_SYNC_UNJUDGED = (
    "copy-sync check could not run: {detail} — without it there is no telling "
    "whether this publication brings {label} in by copy, so it is held; "
    "republish once the repository reads again, or sync the base with a merge "
    "(kew resolve)"
)


def _guard_copy_sync(
    repo: CanonicalRepository,
    artifact: Artifact,
    promoted: PromotedTree,
    *,
    remote: str,
    parents: tuple[str, ...],
    integrated_digest: Callable[[str], str | None] | None,
    merged_base_sha: str | None,
    copy_sync: CopySync | None,
) -> None:
    """Hold a publication that brings the base's content in by copy (#2457).

    Off unless a caller named the base head to judge against, so every
    publication that does not pass one behaves exactly as before. A merge that
    carries THAT HEAD as a parent is never judged: a second parent is the
    legitimate way to bring the base's content in, which is the very thing this
    rule tells the operator to use. A merge of any other commit — notably the
    base kew integrated before the agent ran (#2525), which is a parent of
    every remediation of a branch whose base had moved — accounts for that
    commit alone, so content the base has acquired SINCE is still a copy and
    still judged. Nor is a base that has not moved since the host integrated
    it — the integration already accounts for every byte, and asking costs a
    fetch — or a base head that IS the branch, which holds nothing newer.

    Writes and deletions are both judged (#2633). Every tree this rule reads
    can fail to answer, and on each of them it HOLDS rather than disarms: an
    armed rule that stands down whenever git falters lets the very copy it
    exists to catch through on one transient failure.
    """
    if copy_sync is None:
        return
    integrated_sha = copy_sync.integrated or merged_base_sha
    if copy_sync.sha in (integrated_sha, repo.base_sha):
        return
    if copy_sync.sha in parents[1:] or not promoted.changed_paths:
        return
    if integrated_sha != merged_base_sha:
        integrated_digest = _merged_base_digests(
            repo, remote=remote, merged_base_sha=integrated_sha
        )
    try:
        base, integrated, upstream = _copy_sync_readers(
            repo,
            remote=remote,
            integrated_digest=integrated_digest,
            merged_base_sha=integrated_sha,
            upstream_sha=copy_sync.sha,
        )
        flags = copy_sync_flags(
            artifact.entries,
            published_paths=promoted.changed_paths,
            base_digest=base,
            integrated_digest=integrated,
            upstream_digest=upstream,
            label=copy_sync.label,
            deletions=artifact.deletions,
        )
    except _TreeUnreadable as exc:
        _warn(f"copy-sync rule could not judge, holding: {exc}")
        raise PublicationHeld(
            (_COPY_SYNC_UNJUDGED.format(detail=exc, label=copy_sync.label),)
        ) from exc
    if flags:
        raise PublicationHeld(flags)


def _contains_conflict_block(content: bytes) -> bool:
    if b"\0" in content:
        return False
    stage = 0
    text = content.removeprefix(b"\xef\xbb\xbf").decode(
        "utf-8", errors="surrogateescape"
    )
    for line in text.splitlines():
        if _LONG_CONFLICT_MARKER.match(line):
            return True
        if stage == 0 and _CONFLICT_OPENING.match(line):
            stage = 1
        elif stage == 1 and _CONFLICT_DIVIDER.match(line):
            stage = 2
        elif stage == 2 and _CONFLICT_CLOSING.match(line):
            return True
    return False


def _tree_blobs(
    repo: CanonicalRepository, tree_sha: str
) -> tuple[tuple[str, str], ...]:
    result = run_bytes(
        ["git", "-C", str(repo.path), "ls-tree", "-r", "-z", tree_sha],
        env=_read_env(),
    )
    if result.returncode != 0:
        detail = redact_credentials(
            (result.stderr or b"").decode("utf-8", errors="replace")
        )[:300]
        raise CanonicalRepositoryError(f"could not scan promoted tree: {detail}")
    blobs: list[tuple[str, str]] = []
    for record in (result.stdout or b"").split(b"\0"):
        if not record:
            continue
        metadata, separator, raw_path = record.partition(b"\t")
        fields = metadata.split()
        if separator != b"\t" or len(fields) != 3:
            raise CanonicalRepositoryError("promoted tree listing is malformed")
        if fields[1] != b"blob":
            continue
        path = raw_path.decode("utf-8", errors="surrogateescape")
        oid = fields[2].decode("ascii", errors="strict")
        blobs.append((path, oid))
    return tuple(blobs)


def _batch_blob_size(header: bytes, *, path: str, expected_oid: str) -> int:
    fields = header.rstrip(b"\n").split()
    if len(fields) != 3 or fields[0] != expected_oid.encode() or fields[1] != b"blob":
        raise CanonicalRepositoryError(f"batch response for {path!r} is malformed")
    try:
        size = int(fields[2])
    except ValueError as exc:
        raise CanonicalRepositoryError(
            f"batch response size for {path!r} is malformed"
        ) from exc
    if size > MAX_BLOB_BYTES:
        raise CanonicalRepositoryError(
            f"promoted blob {path!r} is {size} bytes (limit {MAX_BLOB_BYTES})"
        )
    return size


def _stream_batch_blob(stream: IO[bytes], *, path: str, size: int) -> bytes | None:
    remaining = size
    binary = False
    candidate = bytearray()
    while remaining:
        chunk = stream.read(min(64 * 1024, remaining))
        if not chunk:
            raise CanonicalRepositoryError(f"batch blob {path!r} ended early")
        remaining -= len(chunk)
        if not binary and b"\0" in chunk:
            binary = True
            candidate.clear()
        elif not binary:
            candidate.extend(chunk)
    return None if binary else bytes(candidate)


def _read_batch_blob(
    stream: IO[bytes], *, path: str, expected_oid: str
) -> bytes | None:
    size = _batch_blob_size(stream.readline(), path=path, expected_oid=expected_oid)
    content = _stream_batch_blob(stream, path=path, size=size)
    if stream.read(1) != b"\n":
        raise CanonicalRepositoryError(f"batch blob {path!r} has no terminator")
    return content


def _require_no_conflict_blocks(repo: CanonicalRepository, tree_sha: str) -> None:
    process = popen_bytes(
        ["git", "-C", str(repo.path), "cat-file", "--batch"],
        env=_read_env(),
    )
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None
    try:
        for path, oid in _tree_blobs(repo, tree_sha):
            process.stdin.write(f"{oid}\n".encode())
            process.stdin.flush()
            content = _read_batch_blob(process.stdout, path=path, expected_oid=oid)
            if content is not None and _contains_conflict_block(content):
                raise CanonicalRepositoryError(
                    f"the promoted tree still contains conflict markers ({path})"
                )
        process.stdin.close()
        returncode = process.wait()
        if returncode != 0:
            detail = redact_credentials(
                process.stderr.read().decode("utf-8", errors="replace")
            )[:300]
            raise CanonicalRepositoryError(f"could not read promoted blobs: {detail}")
    except BaseException:
        process.kill()
        process.wait()
        raise
    finally:
        process.stdout.close()
        process.stderr.close()


def _push(
    repo: CanonicalRepository,
    *,
    remote: str,
    branch: str,
    commit_sha: str,
    force_with_lease: str | None,
) -> None:
    command = ["git", "-C", str(repo.path), "push"]
    if force_with_lease is not None:
        command.append(f"--force-with-lease={force_with_lease}")
    command.extend(["--", remote, f"{commit_sha}:refs/heads/{branch}"])
    result = run(command, check=False, env=publication_write_env())
    if result.returncode != 0:
        detail = redact_credentials(result.stderr or result.stdout or "").strip()[:300]
        raise PublicationError("push", f"canonical git push failed: {detail}")


def _validate(
    *,
    branch: str,
    parents: tuple[str, ...],
    base_sha: str,
    force_with_lease: str | None,
    replaces: str | None = None,
) -> None:
    if not parents:
        raise CanonicalRepositoryError("publication has no trusted parent")
    _require_oid(base_sha, "base")
    for parent in parents:
        _require_oid(parent, "parent")
    if replaces is not None:
        _require_oid(replaces, "replaced head")
    if force_with_lease is not None:
        expected = {f"{branch}:{parents[0]}", f"{branch}:"}
        if replaces is not None:
            expected = {f"{branch}:{replaces}"}
        if force_with_lease not in expected:
            raise CanonicalRepositoryError(
                "force-with-lease must bind the first parent or exact branch "
                f"absence, got {force_with_lease!r}"
            )


def _push_with_retries(
    repo: CanonicalRepository,
    *,
    remote: str,
    branch: str,
    commit_sha: str,
    force_with_lease: str | None,
    attempts: int,
    delays: tuple[float, ...],
    sleep: Callable[[float], None],
) -> None:
    """Retry only one immutable commit and reconcile ambiguous responses.

    A retry is spent on two answers only: an ambiguous one (the push may have
    landed, so the remote head is read back first) and a *server-side* one, which
    the same commit may survive on the next try. A rejection the remote decided
    from this publication's content or lineage is reported immediately — the
    wait would buy the same answer (#2455).

    Exhausting the retries on a server-side rejection raises
    :class:`TransientPushFailure` rather than a bare push failure: the commit is
    built and the checkout is preserved, so the caller can hold the work for
    another push instead of redoing it.
    """
    if attempts < 1:
        raise ValueError("publication push attempts must be at least one")
    if len(delays) < attempts - 1:
        raise ValueError("publication push delays do not cover every retry")
    last_error: PublicationError | None = None
    for attempt in range(attempts):
        try:
            _push(
                repo,
                remote=remote,
                branch=branch,
                commit_sha=commit_sha,
                force_with_lease=force_with_lease,
            )
            return
        except PublicationError as exc:
            last_error = exc
        if _already_landed(repo, remote=remote, branch=branch, commit_sha=commit_sha):
            return
        if attempt == attempts - 1 or not transient_push_rejection(last_error.detail):
            break
        _warn(
            f"The remote failed the publication push of {branch} on its own side "
            f"(attempt {attempt + 1}/{attempts}), retrying the same commit "
            f"{commit_sha[:12]} in {delays[attempt]:.0f}s: {last_error.detail}"
        )
        sleep(delays[attempt])
    assert last_error is not None
    _raise_push_failure(last_error, attempts=attempts)


def _already_landed(
    repo: CanonicalRepository, *, remote: str, branch: str, commit_sha: str
) -> bool:
    """Whether the remote holds this commit after all — the answer was ambiguous.

    A failed push may have been accepted with its response lost, so the remote's
    own head decides before any retry is spent. A head that cannot be read is
    reported and read as "no": sending the same commit again is the safe way to
    find out, and the remote refuses a duplicate under its lease anyway.
    """
    try:
        observed = remote_branch_sha(remote, branch, scratch=repo.path)
    except PublicationError as exc:
        _warn(f"Cannot reconcile the failed publication push: {exc}")
        return False
    return observed == commit_sha


def _raise_push_failure(error: PublicationError, *, attempts: int) -> NoReturn:
    """Report the push answer no retry left could change.

    A server-side answer is re-raised as :class:`TransientPushFailure` so the
    caller can hold the preserved work for another push instead of failing the
    run that produced it (#2455). Anything else is reported exactly as the push
    reported it.
    """
    if transient_push_rejection(error.detail):
        raise TransientPushFailure(error.detail, attempts=attempts) from error
    raise error


def _prepare(
    checkout_dir: Path,
    *,
    remote: str,
    base_sha: str,
    scratch: Path,
    runtime: str | None,
    image: str | None,
    proxy_environment: str,
) -> tuple[CanonicalRepository, Artifact, PromotedTree]:
    repo = provision(scratch / "canonical", source=remote, base_sha=base_sha)
    artifact = _extract(
        repo,
        checkout_dir,
        scratch / "artifact",
        runtime=runtime,
        image=image,
        proxy_environment=proxy_environment,
    )
    promoted = _promote(repo, artifact, scratch=scratch / "promote")
    return repo, artifact, promoted


def _empty_evidence_artifact(artifact: Artifact, promoted: PromotedTree) -> Artifact:
    """Project a fully filtered no-op into evidence the seal can preserve."""
    if promoted.changed_paths or promoted.entry_count or promoted.deletion_count:
        raise CanonicalRepositoryError(
            "cannot make empty evidence from a publication that has changes"
        )
    return replace(artifact, entries=(), deletions=())


def checkout_changed_paths(
    checkout_dir: Path,
    *,
    remote: str,
    base_sha: str,
    scratch: Path,
) -> tuple[str, ...]:
    """Return filtered checkout changes without invoking git in the checkout."""
    _require_oid(base_sha, "base")
    _require_remote(remote)
    _repo, _artifact, promoted = _prepare(
        checkout_dir,
        remote=remote,
        base_sha=base_sha,
        scratch=scratch,
        runtime=None,
        image=None,
        proxy_environment="none",
    )
    return promoted.changed_paths


def checkout_no_change_evidence(
    checkout_dir: Path,
    *,
    remote: str,
    base_sha: str,
    scratch: Path,
) -> CanonicalNoChange | None:
    """Remote-bound evidence that the checkout adds nothing to *base_sha*, or
    ``None`` when it holds work. Never invokes git in the checkout."""
    _require_oid(base_sha, "base")
    _require_remote(remote)
    _repo, artifact, promoted = _prepare(
        checkout_dir,
        remote=remote,
        base_sha=base_sha,
        scratch=scratch,
        runtime=None,
        image=None,
        proxy_environment="none",
    )
    if promoted.changed_paths:
        return None
    return CanonicalNoChange(
        artifact=_empty_evidence_artifact(artifact, promoted),
        base_sha=base_sha,
        tree_sha=promoted.tree_sha,
    )


def _extraction_policy(
    runtime: str | None, image: str | None, proxy_environment: str | None
) -> str:
    if (runtime is not None or image is not None) and proxy_environment is None:
        raise TypeError("contained extraction requires proxy_environment")
    if (runtime is None) != (image is None):
        raise CanonicalRepositoryError("runtime and image must be supplied together")
    if proxy_environment is not None and proxy_environment not in ("none", "inherit"):
        raise ValueError("proxy_environment must be none or inherit")
    return proxy_environment or "none"


def publish_checkout(
    checkout_dir: Path,
    *,
    remote: str,
    branch: str,
    parents: tuple[str, ...],
    base_sha: str,
    message: str,
    identity: IdentityLike,
    force_with_lease: str | None = None,
    runtime: str | None = None,
    image: str | None = None,
    proxy_environment: str | None = None,
    require_resolved: bool = False,
    publish_empty_tree: bool = False,
    merged_base_sha: str | None = None,
    copy_sync: CopySync | None = None,
    acceptance: tuple[AcceptanceContract, ...] = (),
    on_no_change: Callable[[CanonicalNoChange], None] | None = None,
    on_intent: Callable[[str, str], None] | None = None,
    output_guard: Callable[[OutputScanTarget], None] | None,
    replaces: str | None = None,
    before_push: Callable[[], object] | None = None,
    push_attempts: int = 1,
    push_delays: tuple[float, ...] = (),
    _sleep: Callable[[float], None] = time.sleep,
    scratch: Path,
) -> CanonicalPublication | CanonicalNoChange:
    """Publish a regular-file tree or return remote-bound no-change evidence.

    Contained extraction requires an explicit proxy policy. Host extraction
    permits omission. Runtime and image must be supplied together.
    ``output_guard`` is required by name: it scans the exact commit before the
    intent is recorded or anything reaches the remote (#1969). ``None`` is an
    explicit choice for callers that publish no agent output.

    ``merged_base_sha`` names the base this checkout merged, if it merged one.
    It is read for the review flags only (#2362): a protected path whose bytes
    are already what that base holds was not authored here, so it does not stop
    the publication for a person. Omitted, every protected path flags as before.

    ``copy_sync`` names the base branch's head as the host resolved it, and
    turns on the copy-sync hold (#2457): content that is that head's own, which
    neither the trusted base nor ``merged_base_sha`` holds, was copied in
    rather than merged, and the operator is told which files. Omitted — every
    publication that does not pass one — nothing is judged.

    ``acceptance`` names the node's acceptance contracts as the ledger recorded
    them (#2729). A tree that would not hold each file byte-identical is held
    for a person with reason ``acceptance-test-changed``; nothing is pushed.

    ``before_push`` runs only when there is a new head to push: after the scan,
    before the intent and the push, and never for a no-change result. The host
    clears the human CI approval of an agent PR there (#2033). When it raises,
    nothing is pushed.
    """
    policy = _extraction_policy(runtime, image, proxy_environment)
    _validate(
        branch=branch,
        parents=parents,
        base_sha=base_sha,
        force_with_lease=force_with_lease,
        replaces=replaces,
    )
    _require_remote(remote)
    repo = provision(scratch / "canonical", source=remote, base_sha=base_sha)
    _require_branch(branch, cwd=repo.path)
    _fetch_parents(repo, remote=remote, parents=parents)
    artifact = _extract(
        repo,
        checkout_dir,
        scratch / "artifact",
        runtime=runtime,
        image=image,
        proxy_environment=policy,
    )
    integrated_digest = _merged_base_digests(
        repo, remote=remote, merged_base_sha=merged_base_sha
    )
    promoted = _promote_under_contract(
        repo,
        artifact,
        scratch=scratch / "promote",
        merged_base_digest=integrated_digest,
        acceptance=acceptance,
    )
    if require_resolved:
        _require_no_conflict_blocks(repo, promoted.tree_sha)
    _guard_copy_sync(
        repo,
        artifact,
        promoted,
        remote=remote,
        parents=parents,
        integrated_digest=integrated_digest,
        merged_base_sha=merged_base_sha,
        copy_sync=copy_sync,
    )
    if not promoted.changed_paths and not publish_empty_tree:
        no_change = CanonicalNoChange(
            artifact=_empty_evidence_artifact(artifact, promoted),
            base_sha=base_sha,
            tree_sha=promoted.tree_sha,
        )
        if on_no_change is not None:
            on_no_change(no_change)
        return no_change

    intent = _CommitIntent(
        tree_sha=promoted.tree_sha,
        parent_sha=parents[0],
        extra_parents=parents[1:],
        message=message,
        author=identity,
        committer=identity,
        when=format_datetime(datetime.now(UTC), usegmt=True),
    )
    commit_sha = commit_tree(repo, intent)
    facts = read_commit(repo, commit_sha)
    if facts.tree_sha != promoted.tree_sha or facts.parents != parents:
        raise CanonicalRepositoryError("the canonical commit does not match its intent")
    _guard_output(repo, output_guard, parents=parents, commit_sha=commit_sha)
    if before_push is not None:
        before_push()
    if on_intent is not None:
        # Durable binding of THIS publication's commit before anything reaches
        # the remote, so a retry can tell its own branch from a stale one.
        on_intent(commit_sha, promoted.tree_sha)
    _push_with_retries(
        repo,
        remote=remote,
        branch=branch,
        commit_sha=commit_sha,
        force_with_lease=force_with_lease,
        attempts=push_attempts,
        delays=push_delays,
        sleep=_sleep,
    )
    return CanonicalPublication(
        commit_sha=commit_sha,
        tree_sha=promoted.tree_sha,
        parent_shas=parents,
        changed_paths=promoted.changed_paths,
        flags=(),
    )


def _guard_output(
    repo: CanonicalRepository,
    output_guard: Callable[[OutputScanTarget], None] | None,
    *,
    parents: tuple[str, ...],
    commit_sha: str,
    already_published: bool = False,
) -> None:
    """Run the caller's credential scan on the exact commit about to be pushed.

    ``already_published`` is true only for a head the remote already holds
    because the agent pushed it; a commit the host builds is never marked.
    """
    if output_guard is None:
        return
    output_guard(
        _scan_target(
            repo,
            parents=parents,
            commit_sha=commit_sha,
            already_published=already_published,
        )
    )


def _scan_target(
    repo: CanonicalRepository,
    *,
    parents: tuple[str, ...],
    commit_sha: str,
    already_published: bool,
) -> OutputScanTarget:
    return OutputScanTarget(
        repository=repo.path,
        base_sha=parents[0],
        head_sha=commit_sha,
        other_parents=parents[1:],
        env=_read_env(),
        already_published=already_published,
    )


def _agent_range_depths() -> tuple[int, ...]:
    """Fetch depths from the agent head, doubling up to the commit bound."""
    depths: list[int] = []
    depth = _AGENT_RANGE_FIRST_DEPTH
    while depth <= AGENT_RANGE_MAX_COMMITS:
        depths.append(depth)
        depth *= 2
    # One more than the bound: the oldest commit's parent (the reviewed head)
    # must arrive too, or that commit stays a shallow boundary.
    depths.append(AGENT_RANGE_MAX_COMMITS + 1)
    return tuple(depths)


def _complete_agent_range(
    repo: CanonicalRepository, *, remote: str, expected_sha: str, observed_sha: str
) -> None:
    """Deepen the agent head's history until the scan can read all of it.

    :func:`provision` and :func:`fetch_commit` fetch one commit deep, so the
    agent head arrives as a shallow root and ``rev-list`` would list it alone:
    no intermediate commit, message or removed blob. This fetches more of its
    history, bounded in commits and time, until no commit between the reviewed
    head and the agent head is a shallow boundary. It never raises for a range
    it could not complete: the scan refuses that range as unavailable
    (review round 11).
    """
    target = _scan_target(
        repo,
        parents=(expected_sha,),
        commit_sha=observed_sha,
        already_published=True,
    )
    deadline = time.monotonic() + BASE_SCAN_TIMEOUT_SECONDS
    try:
        for depth in _agent_range_depths():
            if not range_truncated(target, deadline):
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            fetch_commit(
                repo, source=remote, sha=observed_sha, depth=depth, timeout=remaining
            )
    except (CanonicalRepositoryError, OutputScanError) as exc:
        _warn(f"Cannot fetch the agent-pushed history to scan it: {exc}")


def _reset_to_reviewed_head(
    repo: CanonicalRepository,
    *,
    remote: str,
    branch: str,
    expected_sha: str,
    observed_sha: str,
    scratch: Path,
) -> None:
    try:
        _push(
            repo,
            remote=remote,
            branch=branch,
            commit_sha=expected_sha,
            force_with_lease=f"{branch}:{observed_sha}",
        )
    except PublicationError as exc:
        latest = remote_branch_sha(remote, branch, scratch=scratch / "reset-query")
        if latest == expected_sha:
            return
        raise PublicationError(
            "push",
            f"remote branch {branch!r} moved to {latest[:12] or '(gone)'} while "
            f"being reset to the reviewed head {expected_sha[:12]}; checkout "
            "preserved",
        ) from exc


def republish_agent_head(
    checkout_dir: Path,
    *,
    remote: str,
    branch: str,
    expected_sha: str,
    observed_sha: str,
    message: str,
    identity: IdentityLike,
    output_guard: Callable[[OutputScanTarget], None] | None,
    scratch: Path,
    acceptance: tuple[AcceptanceContract, ...] = (),
) -> CanonicalPublication:
    """Replace an agent-pushed head with a host-built commit of the same tree.

    A self-publishing review runs with the agent holding the push credential,
    so the branch may now carry the agent's own commit objects, including any
    history it merged in. The host never adopts that graph. It proves, in a
    fresh host-owned repository, that ``observed_sha`` carries exactly the tree
    the checkout promotes to on top of ``expected_sha``; then it publishes one
    host commit parented by ``expected_sha`` with that tree, under a lease bound
    to ``observed_sha`` (review round 4). A head whose tree differs is another
    writer's work or an unpushed remediation, and is refused with the checkout
    preserved.

    ``acceptance`` is held exactly as :func:`publish_checkout` holds it
    (#2729): the host adopts no tree that changes the node's acceptance test.
    """
    _require_oid(expected_sha, "expected head")
    _require_oid(observed_sha, "observed head")
    _require_remote(remote)
    repo = provision(scratch / "canonical", source=remote, base_sha=expected_sha)
    _require_branch(branch, cwd=repo.path)
    _fetch_parents(repo, remote=remote, parents=(observed_sha,))
    # The agent already pushed these commits: scan what it put on the branch
    # (its own commit headers included) before the host builds anything. The
    # whole range from the reviewed head, not only the head's final tree.
    if output_guard is not None:
        _complete_agent_range(
            repo, remote=remote, expected_sha=expected_sha, observed_sha=observed_sha
        )
    _guard_output(
        repo,
        output_guard,
        parents=(expected_sha,),
        commit_sha=observed_sha,
        already_published=True,
    )
    artifact = _extract(
        repo,
        checkout_dir,
        scratch / "artifact",
        runtime=None,
        image=None,
        proxy_environment="none",
    )
    promoted = _promote_under_contract(
        repo,
        artifact,
        scratch=scratch / "promote",
        merged_base_digest=None,
        acceptance=acceptance,
    )
    facts = read_commit(repo, observed_sha)
    if facts.tree_sha != promoted.tree_sha:
        raise PublicationError(
            "push",
            f"remote branch {branch!r} head {observed_sha[:12]} does not carry the "
            "checkout's tree, so the remediation is not proven published; "
            "checkout preserved",
        )
    if not promoted.changed_paths:
        # Change-then-revert: the agent's commits carry no tree change, and
        # they must not stay reachable either. Reset the branch to the
        # reviewed head under the observed-head lease (review round 5); a
        # lease rejection is re-queried, and a branch that already sits at
        # the reviewed head is the desired state (round 6).
        _reset_to_reviewed_head(
            repo,
            remote=remote,
            branch=branch,
            expected_sha=expected_sha,
            observed_sha=observed_sha,
            scratch=scratch,
        )
        return CanonicalPublication(
            commit_sha=expected_sha,
            tree_sha=promoted.tree_sha,
            parent_shas=(),
            changed_paths=(),
            flags=("reset-to-reviewed-head",),
        )
    intent = _CommitIntent(
        tree_sha=promoted.tree_sha,
        parent_sha=expected_sha,
        message=message,
        author=identity,
        committer=identity,
        when=format_datetime(datetime.now(UTC), usegmt=True),
    )
    commit_sha = commit_tree(repo, intent)
    published = read_commit(repo, commit_sha)
    if published.tree_sha != promoted.tree_sha or published.parents != (expected_sha,):
        raise CanonicalRepositoryError("the canonical commit does not match its intent")
    _guard_output(repo, output_guard, parents=(expected_sha,), commit_sha=commit_sha)
    _push(
        repo,
        remote=remote,
        branch=branch,
        commit_sha=commit_sha,
        force_with_lease=f"{branch}:{observed_sha}",
    )
    return CanonicalPublication(
        commit_sha=commit_sha,
        tree_sha=promoted.tree_sha,
        parent_shas=(expected_sha,),
        changed_paths=promoted.changed_paths,
        flags=(),
    )
