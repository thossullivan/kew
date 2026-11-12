"""Reading the container's artifact as opaque bytes (W1.1 / D3).

The broker's rule is that **the host never points git at an agent-writable
directory**, and never lets an agent-chosen name decide where the host writes.
Extraction therefore happens inside the container, which produces an artifact;
this module is the host's reader for it.

## Layout

    <artifact>/manifest.json      {"scope": "workspace", "base": "<sha>",
                                   "entries": [...], "deletions": [...]}
    <artifact>/blobs/<sha256>     file content, addressed by its own hash

## Why an empty package has to prove itself

``scope`` states that the producer looked at the whole workspace — committed,
staged, unstaged and untracked work — rather than only at commits. It matters
for exactly one artifact: the empty one. "The agent changed nothing" and "the
agent never ran git commit, and the producer only reads commits" are the same
bytes on disk, and the second is silent data loss dressed as a clean run. So an
empty package that does not declare its scope is refused here.

## Why content is addressed by hash, not by path

The manifest's paths are attacker-controlled. If the artifact stored content at
those paths, then merely *unpacking* it would already have written wherever the
attacker chose — before any filter had an opinion. Content-addressing makes the
path pure data: it is carried, filtered, and only becomes a filesystem
operation after :func:`kew.publication_filter.filter_manifest` approves it.

So this module opens exactly one shape of name — 64 hex characters — and a
path from the manifest never reaches an ``open()`` here.

## Why the host re-hashes

The manifest is written by the container. Its ``sha256`` fields are claims, not
evidence. The host computes the digest itself and compares; a manifest that
describes content it did not produce is refused rather than believed.

## The artifact directory is agent-writable too

It is produced into the mounted checkout, so every entry in it — including
``manifest.json`` and ``blobs/`` themselves — could be a symlink pointing out of
the container's world and into the operator's. Every open here is
``O_NOFOLLOW`` and every directory is lstat-checked.

**No callers yet, by construction** — spec node 4.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat as stat_module
from dataclasses import dataclass
from pathlib import Path

from kew.publication_filter import ALLOWED_MODES, ManifestEntry

__all__ = [
    "PACKAGE_DIGEST_VERSION",
    "Artifact",
    "ArtifactRejected",
    "package_digest",
    "read_artifact",
]

MANIFEST_NAME = "manifest.json"
BLOBS_DIR = "blobs"

#: What a package digest COVERS. Bump it whenever the preimage changes shape,
#: so a digest computed under the old rules can never be read as one computed
#: under the new ones — the two would name different things by the same name.
PACKAGE_DIGEST_VERSION = 1
_DIGEST_PREFIX = "sha256:"

#: The scope a producer declares when the package spans the whole workspace —
#: committed, staged, unstaged and untracked work as one state. Anything else
#: is a producer that can only see commits, and an agent that edited files
#: without committing them looks identical to one that did nothing.
WORKSPACE_SCOPE = "workspace"
KNOWN_SCOPES = frozenset({WORKSPACE_SCOPE})

# The only filename shape this module will open inside blobs/. A name that is
# not exactly a sha256 digest cannot be a traversal, an absolute path, a device
# name, or anything else — the constraint is the defence, not a denylist.
# Matched with fullmatch(), never match(): Python's `$` also matches just BEFORE
# a trailing newline, so `match()` would accept "<40 hex>\n" as a commit id and
# "<64 hex>\n" as a blob name. The anchors are kept only for readability.
_BLOB_NAME = re.compile(r"[0-9a-f]{64}")
_SHA1_OR_SHA256 = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")

# Bounds. An artifact is produced by an agent that may be trying to exhaust the
# host rather than exploit it; refusing a stupid size is cheaper than reading
# it. All generous for real work.
MAX_MANIFEST_BYTES = 8 * 1024 * 1024
MAX_ENTRIES = 20_000
MAX_BLOB_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 512 * 1024 * 1024
_HASH_CHUNK = 1024 * 1024


class ArtifactRejected(Exception):
    """The artifact is not well-formed. Never a partial read — always total."""


@dataclass(frozen=True)
class Artifact:
    """A verified artifact. Every blob's digest has been recomputed by the host."""

    base_sha: str
    entries: tuple[ManifestEntry, ...]
    blobs_dir: Path
    #: Paths the agent REMOVED. Carried separately from ``entries`` because a
    #: deletion has no mode and no content — folding it in as a sentinel mode
    #: would mean widening the mode allowlist, which is the security core of
    #: the filter. These paths are still attacker-controlled and get exactly
    #: the same path rules as an entry.
    deletions: tuple[str, ...] = ()
    #: What the producer says the package covers, or ``None`` from a producer
    #: that declares nothing. An EMPTY package must declare
    #: :data:`WORKSPACE_SCOPE`; see :func:`_parse_scope`.
    scope: str | None = None
    #: Untracked dependency-install directories (``node_modules``, ``.venv``)
    #: the host walker did not enter, so the omission is on the record rather
    #: than silent. Not part of the manifest: only the host walker sets it.
    omitted_dependency_directories: tuple[str, ...] = ()
    #: Untracked paths the BASE commit's ``.gitignore`` files ignore, which the
    #: host walker left out: a directory it did not enter, or a single file.
    #: Not part of the manifest: only the host walker sets it (#2108).
    omitted_ignored_paths: tuple[str, ...] = ()
    #: Untracked directories that are themselves a git worktree or repository
    #: — they hold a ``.git`` git would open as one — which the host walker did
    #: not enter (#2201). Not part of the manifest: only the host walker sets it.
    omitted_nested_repositories: tuple[str, ...] = ()

    def content(self, entry: ManifestEntry) -> bytes:
        """The bytes for *entry*, opened by DIGEST — never by its path."""
        return _read_blob(self.blobs_dir, entry.sha256)

    def text_for(self, path: str) -> str:
        """UTF-8 text for the entry at *path*, for the filter's content checks.

        Plugs into ``filter_manifest(read_text=...)``. Lookup is by path because
        that is what the filter knows, but the path is used only to find the
        DIGEST — the open is still by digest, so an attacker-chosen name never
        reaches the filesystem.
        """
        for entry in self.entries:
            if entry.path == path:
                return self.content(entry).decode("utf-8", errors="strict")
        raise ArtifactRejected(f"no artifact entry for {path!r}")


def _open_regular(path: Path, what: str, *, dir_fd: int | None = None) -> int:
    """Open *path* and prove from the DESCRIPTOR that it is a regular file.

    Checking the path with ``lstat`` and then opening it separately decides on
    one object and reads another: swap a regular file for a directory in
    between and ``read()`` raises ``IsADirectoryError``, a crash where this
    module owes a refusal. ``fstat`` on the open descriptor closes that window,
    because it can only describe the thing actually opened.

    ``O_NONBLOCK`` is as load-bearing as ``O_NOFOLLOW``: opening a FIFO for
    reading blocks until a writer appears, so a named pipe here would hang the
    host rather than be refused, and a FIFO is not a symlink.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    except OSError as exc:
        raise ArtifactRejected(f"{what} could not be opened: {exc}") from exc
    try:
        if not stat_module.S_ISREG(os.fstat(fd).st_mode):
            raise ArtifactRejected(f"{what} is not a regular file")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _read_manifest_bytes(artifact_dir: Path) -> bytes:
    """The manifest's bytes, size-bounded from the descriptor that is read."""
    fd = _open_regular(artifact_dir / MANIFEST_NAME, MANIFEST_NAME)
    try:
        size = os.fstat(fd).st_size
        if size > MAX_MANIFEST_BYTES:
            raise ArtifactRejected(
                f"{MANIFEST_NAME} is {size} bytes (limit {MAX_MANIFEST_BYTES})"
            )
    except BaseException:
        os.close(fd)
        raise
    with os.fdopen(fd, "rb") as fh:
        return fh.read(MAX_MANIFEST_BYTES + 1)


def _open_blobs_dir(artifact_dir: Path) -> int:
    """A descriptor for ``blobs/``, so later opens cannot be re-routed.

    ``lstat`` on the path and then opening children BY PATH leaves a window:
    replace ``blobs/`` with a symlink in between and enumeration walks out of
    the artifact entirely. Opening the directory once and resolving every child
    through ``dir_fd`` removes the second lookup rather than racing it.
    """
    try:
        fd = os.open(
            artifact_dir / BLOBS_DIR, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )
    except OSError as exc:
        raise ArtifactRejected(f"{BLOBS_DIR}/ could not be opened: {exc}") from exc
    return fd


def _open_blob(blobs_dir: Path | int, digest: str) -> tuple[int, int]:
    """Open one blob and prove it is a bounded regular file. Returns (fd, size).

    *blobs_dir* is a directory DESCRIPTOR during verification, so the name is
    resolved relative to the directory already proven, and a path afterwards
    for the re-hashing reads (where wrong content cannot survive the digest
    comparison anyway).
    """
    if not _BLOB_NAME.fullmatch(digest):
        raise ArtifactRejected(f"blob name {digest!r} is not a sha256 digest")
    if isinstance(blobs_dir, int):
        fd = _open_regular(Path(digest), f"blob {digest}", dir_fd=blobs_dir)
    else:
        fd = _open_regular(blobs_dir / digest, f"blob {digest}")
    size = os.fstat(fd).st_size
    if size > MAX_BLOB_BYTES:
        os.close(fd)
        raise ArtifactRejected(
            f"blob {digest} is {size} bytes (limit {MAX_BLOB_BYTES})"
        )
    return fd, size


def _read_blob(blobs_dir: Path, digest: str) -> bytes:
    """The blob's bytes, re-verified against *digest* on every read.

    Verification at read time, not only at load time: the blobs live in a
    directory the agent can write, so anything validated once and reopened
    later has a window in between. Re-hashing closes it — the bytes a caller
    receives always hash to the digest it asked for, or it gets a refusal.
    """
    fd, _size = _open_blob(blobs_dir, digest)
    with os.fdopen(fd, "rb") as fh:
        payload = fh.read(MAX_BLOB_BYTES + 1)
    if len(payload) > MAX_BLOB_BYTES:
        raise ArtifactRejected(f"blob {digest} grew past {MAX_BLOB_BYTES} bytes")
    actual = hashlib.sha256(payload).hexdigest()
    if actual != digest:
        raise ArtifactRejected(f"blob {digest!r} now hashes to {actual!r}")
    return payload


def _digest_of(blobs_dir: Path | int, digest: str) -> tuple[str, int]:
    """Recompute a blob's digest, streaming and BOUNDED.

    The pre-open size is not a bound on what a read will return: a blob the
    agent is still writing keeps the loop fed, so hashing "until EOF" has no
    end. Bytes are counted as they are read and the limit is enforced against
    the count, and the COUNT is what the caller gets back — a growth race
    otherwise reports a smaller size than was actually read, quietly
    understating the aggregate.
    """
    fd, _size = _open_blob(blobs_dir, digest)
    hasher = hashlib.sha256()
    read = 0
    with os.fdopen(fd, "rb") as fh:
        while chunk := fh.read(_HASH_CHUNK):
            read += len(chunk)
            if read > MAX_BLOB_BYTES:
                raise ArtifactRejected(
                    f"blob {digest} exceeded {MAX_BLOB_BYTES} bytes while being read"
                )
            hasher.update(chunk)
    return hasher.hexdigest(), read


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ArtifactRejected(message)


def _parse_entry(raw: object, index: int) -> ManifestEntry:
    """One manifest row, with every field's TYPE checked.

    A JSON document is not a schema. Without this, a path of ``null`` or a mode
    of ``{}`` would reach the filter's string operations and fail there as a
    crash rather than here as a refusal.
    """
    _require(isinstance(raw, dict), f"entry {index} is not an object")
    assert isinstance(raw, dict)
    path, mode, sha256 = raw.get("path"), raw.get("mode"), raw.get("sha256")
    _require(isinstance(path, str) and path != "", f"entry {index} has no path")
    # Validate every row before reconciling blob references. A malformed row
    # must name its own defect even when the blobs directory has other content.
    _require(
        isinstance(mode, str) and mode in ALLOWED_MODES,
        f"entry {index} has mode {mode!r}",
    )
    _require(
        isinstance(sha256, str) and bool(_BLOB_NAME.fullmatch(sha256)),
        f"entry {index} has no sha256 digest",
    )
    assert isinstance(path, str) and isinstance(mode, str) and isinstance(sha256, str)
    return ManifestEntry(path=path, mode=mode, sha256=sha256)


def _parse_deletions(document: dict[str, object]) -> tuple[str, ...]:
    """The removed paths, absent-means-empty for artifacts that delete nothing."""
    raw = document.get("deletions", [])
    _require(isinstance(raw, list), "manifest 'deletions' is not a list")
    assert isinstance(raw, list)
    _require(len(raw) <= MAX_ENTRIES, f"manifest deletes more than {MAX_ENTRIES} paths")
    for index, path in enumerate(raw):
        _require(
            isinstance(path, str) and path != "",
            f"deletion {index} is not a path",
        )
    return tuple(raw)


def _parse_scope(
    document: dict[str, object],
    entries: tuple[ManifestEntry, ...],
    deletions: tuple[str, ...],
) -> str | None:
    """What the producer says it covered, and whether silence is admissible.

    An empty package is the one artifact whose meaning cannot be read off its
    own contents: "the agent changed nothing" and "the producer only knew how
    to look at commits, and the agent never committed" produce byte-identical
    manifests. The declaration is what tells them apart, so an empty package
    without it is refused rather than published as a verified no-change.

    A non-empty package is self-evidencing — it carries work — so the field is
    optional there, and only its VALUE is checked. That keeps the refusal aimed
    at the case that is actually ambiguous instead of at every older artifact.
    """
    raw = document.get("scope")
    if raw is not None:
        _require(
            isinstance(raw, str) and raw in KNOWN_SCOPES,
            f"manifest 'scope' is {raw!r}",
        )
    if not entries and not deletions:
        _require(
            raw == WORKSPACE_SCOPE,
            "an empty package must declare that it covers the whole workspace; "
            f"this one declares {raw!r}, so it cannot be read as a no-change",
        )
    assert raw is None or isinstance(raw, str)
    return raw


def _parse_manifest(
    payload: bytes,
) -> tuple[str, tuple[ManifestEntry, ...], tuple[str, ...], str | None]:
    _require(
        len(payload) <= MAX_MANIFEST_BYTES, f"{MANIFEST_NAME} exceeds the size limit"
    )
    try:
        document = json.loads(payload.decode("utf-8"))
    # RecursionError from deeply nested arrays and ValueError from an
    # oversized integer literal are not JSONDecodeError, so both escaped as
    # crashes from a boundary that owes a refusal.
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise ArtifactRejected(
            f"{MANIFEST_NAME} is not valid UTF-8 JSON: {exc}"
        ) from exc

    _require(isinstance(document, dict), f"{MANIFEST_NAME} is not an object")
    assert isinstance(document, dict)
    base = document.get("base")
    _require(
        isinstance(base, str) and bool(_SHA1_OR_SHA256.fullmatch(base)),
        "manifest 'base' is not a commit id",
    )
    rows = document.get("entries")
    _require(isinstance(rows, list), "manifest 'entries' is not a list")
    assert isinstance(rows, list) and isinstance(base, str)
    _require(len(rows) <= MAX_ENTRIES, f"manifest has more than {MAX_ENTRIES} entries")

    entries = tuple(_parse_entry(row, i) for i, row in enumerate(rows))
    deletions = _parse_deletions(document)
    return base, entries, deletions, _parse_scope(document, entries, deletions)


def _verify_blobs(artifact_dir: Path, entries: tuple[ManifestEntry, ...]) -> None:
    """Every entry has a blob whose ACTUAL digest matches, and nothing is spare.

    An unreferenced blob is refused rather than ignored: content the manifest
    does not account for means the artifact is not what it says it is, and
    "skip what we did not expect" is how a second payload rides along.
    """
    blobs_fd = _open_blobs_dir(artifact_dir)
    try:
        referenced = {e.sha256 for e in entries}
        _reject_spare_blobs(blobs_fd, referenced)
        sizes = _verify_referenced(blobs_fd, referenced)
    finally:
        os.close(blobs_fd)
    # Aggregate over ENTRIES, not unique digests: 20,000 paths can share one
    # 64 MiB blob, and counting the digest once bounds the artifact at 64 MiB
    # while the work of applying it is measured in terabytes.
    total = sum(sizes[e.sha256] for e in entries)
    _require(
        total <= MAX_TOTAL_BYTES,
        f"artifact expands to {total} bytes of content (limit {MAX_TOTAL_BYTES})",
    )


def _reject_spare_blobs(blobs_fd: int, referenced: set[str]) -> None:
    """Refuse the first unreferenced name, without listing the directory first.

    ``MAX_ENTRIES`` bounds what the MANIFEST may reference; it says nothing
    about how many files the agent put in ``blobs/``. Materialising the whole
    listing to compare sets means a directory of millions of names is walked in
    full before the first spare one is refused.
    """
    try:
        with os.scandir(blobs_fd) as listing:
            for count, item in enumerate(listing, start=1):
                if item.name not in referenced:
                    raise ArtifactRejected(
                        f"blob {item.name!r} is not referenced by the manifest"
                    )
                _require(
                    count <= MAX_ENTRIES,
                    f"{BLOBS_DIR}/ holds more than {MAX_ENTRIES} files",
                )
    except OSError as exc:
        raise ArtifactRejected(f"{BLOBS_DIR}/ could not be read: {exc}") from exc


def _verify_referenced(blobs_fd: int, referenced: set[str]) -> dict[str, int]:
    """Hash each referenced blob ONCE and keep its verified size."""
    sizes: dict[str, int] = {}
    for digest in sorted(referenced):
        actual, size = _digest_of(blobs_fd, digest)
        _require(actual == digest, f"blob {digest!r} hashes to {actual!r}")
        sizes[digest] = size
    return sizes


def package_digest(artifact: Artifact) -> str:
    """A ``sha256:<hex>`` name for exactly the work *artifact* carries (I07).

    This is the identity every later link of the lineage is bound to, so it has
    to answer one question and only that one: **is this the same work?**

    The preimage is therefore the package's MEANING and nothing else — the
    declared scope, the trusted base it applies to, the (path, mode, digest) of
    every entry, and the set of removed paths. Entries are sorted, so a
    producer that enumerates the workspace in a different order the second time
    produces the same package rather than a new one; nothing derived from *when*
    the producer ran is in the preimage at all, which is what makes a re-run
    recoverable rather than merely similar.

    Equally, everything that changes the meaning is in it. A different base, a
    different mode, one more deletion, or a package that declares no scope where
    another declared ``workspace`` all name a different package, because
    publishing any of them would publish something else.
    """
    preimage = json.dumps(
        [
            PACKAGE_DIGEST_VERSION,
            artifact.scope,
            artifact.base_sha,
            sorted([e.path, e.mode, e.sha256] for e in artifact.entries),
            sorted(artifact.deletions),
        ],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return _DIGEST_PREFIX + hashlib.sha256(preimage.encode("utf-8")).hexdigest()


def read_artifact(artifact_dir: Path) -> Artifact:
    """Read and verify the container's artifact. Raises on anything malformed.

    Verification is TOTAL: a single bad entry refuses the artifact rather than
    yielding a usable subset, matching the filter's own contract. The result
    still has to go through :func:`kew.publication_filter.filter_manifest` —
    this establishes that the artifact is well-formed and internally honest,
    not that its paths are safe to write.
    """
    base, entries, deletions, scope = _parse_manifest(
        _read_manifest_bytes(artifact_dir)
    )
    blobs_dir = artifact_dir / BLOBS_DIR
    _verify_blobs(artifact_dir, entries)
    return Artifact(
        base_sha=base,
        entries=entries,
        blobs_dir=blobs_dir,
        deletions=deletions,
        scope=scope,
    )
