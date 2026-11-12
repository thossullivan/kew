"""Durable, host-owned placement for a verified work package (I07).

The package the container produced lives in the mounted checkout — a directory
the agent can write for as long as it exists. Every fact the rest of this epic
records is a claim about *those bytes*: the tree they build, the receipt that
seals them, the audit prefix the receipt closes. A claim about bytes an agent
can still edit is not evidence, so the first move is to stop the bytes being
agent-reachable.

## What "durable" means here

* The store root is created ``0o700`` and refused if it is a symlink or is
  owned by anyone but this process. A copy placed somewhere the agent can also
  reach would have preserved nothing.
* A placement is atomic. Content is staged in a sibling directory and renamed
  into place in one step, so a crash leaves either a complete package or a
  discardable staging directory — never a half-written one that reads as whole.
* Files land ``0o400``. That is a guard rail, not the guarantee: the guarantee
  is that every read re-hashes, so an edit made with the owner's own privileges
  is refused rather than returned.

## Why the name is the content

A package is addressed by :func:`kew.publication_artifact.package_digest` — a
digest over the package's meaning, not over the producer's run. "The same work"
and "the same directory" therefore become one statement, which is what makes a
re-run after a crash *recover* the package rather than place a second copy of
it. It is also the whole of this module's path safety: only 64 hex characters
ever join onto the root, so a manifest path — still attacker-controlled — has
no way to reach the filesystem here.

## What this module deliberately does not do

It records nothing. Indexing the placement (``run_artifact``) and sealing it
(:mod:`kew.artifact_receipt`) are separate steps on purpose: placement is the
only part that can be safely retried after a crash, because it is idempotent by
construction, and folding a database write into it would make it not so.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from kew.publication_artifact import (
    BLOBS_DIR,
    MANIFEST_NAME,
    Artifact,
    ArtifactRejected,
    package_digest,
    read_artifact,
)

__all__ = [
    "PACKAGE_ARTIFACT_KIND",
    "PACKAGE_SUBPATH",
    "PackageStore",
    "PackageStoreError",
    "PackageTampered",
    "PackageUnavailable",
    "PreservedPackage",
    "default_root",
]

PACKAGE_ARTIFACT_KIND = "work_package"
"""The ``run_artifact`` kind a preserved package is indexed under."""

PACKAGE_SUBPATH = (".kew", "private", "packages")
"""Where preserved packages live, relative to the project root."""

_PACKAGES = "packages"
_STAGING = ".staging"
_DIR_MODE = 0o700
_FILE_MODE = 0o400
_DIGEST_PREFIX = "sha256:"
_HEX = re.compile(r"[0-9a-f]{64}")
_CREATE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW


class PackageStoreError(RuntimeError):
    """The package could not be placed, named, or found. Never a partial read."""


class PackageUnavailable(PackageStoreError):
    """No package is preserved under this digest. Distinct from an empty one."""


class PackageTampered(PackageStoreError):
    """The preserved bytes are no longer the bytes that were preserved."""


@dataclass(frozen=True)
class PreservedPackage:
    """Where a package is, and what it is — metadata only, never the bytes."""

    digest: str
    root: Path
    base_sha: str
    scope: str | None
    entry_count: int
    deletion_count: int
    byte_length: int

    @property
    def locator(self) -> str:
        """The opaque store-relative name, for the ``run_artifact`` index."""
        return f"{_PACKAGES}/{self.digest.removeprefix(_DIGEST_PREFIX)}"


def default_root(project_root: Path | str) -> Path:
    """The preserved-package root for *project_root*."""
    return Path(project_root).joinpath(*PACKAGE_SUBPATH)


def _hex_of(digest: object) -> str:
    """The one name shape that may join onto the store root.

    Every reference reaching this module — including one read back out of the
    database — is checked here before it becomes a path. A digest cannot be a
    traversal, an absolute path, or a device name, so the constraint is the
    defence rather than a denylist of the things it happens to exclude.
    """
    if not isinstance(digest, str) or not digest.startswith(_DIGEST_PREFIX):
        raise PackageStoreError(f"package reference {digest!r} is not a sha256 digest")
    body = digest.removeprefix(_DIGEST_PREFIX)
    if not _HEX.fullmatch(body):
        raise PackageStoreError(f"package reference {digest!r} is not a sha256 digest")
    return body


def _ensure_owner_only(path: Path) -> None:
    """Create *path* ``0o700``, and prove nobody else owns or can write it."""
    if path.is_symlink():
        raise PackageStoreError(f"package store path is a symlink: {path}")
    try:
        path.mkdir(parents=True, mode=_DIR_MODE, exist_ok=True)
        # 0o700 TIGHTENS a pre-existing directory and never widens one, which is
        # the opposite of the "0o644 is a good default" advice: preserved
        # evidence is exactly what nobody but the owner may read or replace.
        os.chmod(path, _DIR_MODE)  # noqa: S103,E501  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions
    except OSError as exc:
        raise PackageStoreError(f"cannot prepare {path}: {exc}") from exc
    info = path.lstat()
    if info.st_uid != os.geteuid():
        raise PackageStoreError(
            f"{path} is owned by uid {info.st_uid}, not the host process"
        )


def _write_exclusive(path: Path, data: bytes) -> None:
    """Create *path* with ``O_EXCL|O_NOFOLLOW`` at ``0o400`` and flush it."""
    try:
        handle = os.open(path, _CREATE_FLAGS, _FILE_MODE)
    except OSError as exc:
        raise PackageStoreError(f"refused to create {path.name}: {exc}") from exc
    try:
        os.write(handle, data)
        os.fsync(handle)
    except OSError as exc:
        raise PackageStoreError(f"could not write {path.name}: {exc}") from exc
    finally:
        os.close(handle)


def _fsync_dir(path: Path) -> None:
    """Flush a directory entry, so the rename survives the power going out."""
    try:
        handle = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    except OSError as exc:
        raise PackageStoreError(f"could not open {path} to flush it: {exc}") from exc
    try:
        os.fsync(handle)
    finally:
        os.close(handle)


def _canonical_manifest(artifact: Artifact) -> bytes:
    """The HOST's own manifest bytes for *artifact*.

    The container's manifest is never copied. It is agent-written JSON that may
    carry fields nothing verified, and preserving it would make "what the host
    believes" and "what the agent wrote" the same file. This re-serializes only
    the facts :func:`read_artifact` proved, in the order
    :func:`package_digest` fixes — so the stored package re-derives its own
    name.
    """
    document: dict[str, object] = {
        "base": artifact.base_sha,
        "entries": [
            {"path": path, "mode": mode, "sha256": sha}
            for path, mode, sha in sorted(
                [e.path, e.mode, e.sha256] for e in artifact.entries
            )
        ],
        "deletions": sorted(artifact.deletions),
    }
    if artifact.scope is not None:
        document["scope"] = artifact.scope
    return json.dumps(document, ensure_ascii=True, separators=(",", ":")).encode()


class PackageStore:
    """Owner-only, content-addressed placement for verified work packages."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        _ensure_owner_only(self.root)
        self._packages = self.root / _PACKAGES
        self._staging = self.root / _STAGING
        _ensure_owner_only(self._packages)
        _ensure_owner_only(self._staging)

    def preserve(self, artifact: Artifact) -> PreservedPackage:
        """Place *artifact* durably and return where it landed.

        Idempotent by construction: the destination is the package's own
        digest, so preserving the same work twice — including the retry after a
        crash — converges on one directory instead of accumulating copies.
        """
        digest = package_digest(artifact)
        destination = self._packages / _hex_of(digest)
        if destination.exists():
            return self.describe(digest)
        staged = self._stage(artifact, digest)
        try:
            os.rename(staged, destination)
        except OSError as exc:
            shutil.rmtree(staged, ignore_errors=True)
            if destination.exists():
                # Another host process placed the same package while this one
                # was staging it. Same bytes, same name — nothing was lost.
                return self.describe(digest)
            raise PackageStoreError(f"could not place package {digest}: {exc}") from exc
        _fsync_dir(self._packages)
        return self.describe(digest)

    def load(self, digest: str) -> Artifact:
        """The preserved package, re-verified from disk on every read.

        :func:`read_artifact` re-hashes every blob, and the package digest is
        re-derived from what it found. An edit made after placement — by the
        owner, since nobody else can — is therefore refused rather than handed
        back as evidence.
        """
        destination = self._packages / _hex_of(digest)
        if not destination.is_dir():
            raise PackageUnavailable(f"no package is preserved under {digest}")
        try:
            artifact = read_artifact(destination)
        except ArtifactRejected as exc:
            raise PackageTampered(
                f"preserved package {digest} no longer reads as one: {exc}"
            ) from exc
        actual = package_digest(artifact)
        if actual != digest:
            raise PackageTampered(f"preserved package {digest} now names {actual}")
        return artifact

    def describe(self, digest: str) -> PreservedPackage:
        """Metadata for a preserved package, after re-verifying it."""
        artifact = self.load(digest)
        destination = self._packages / _hex_of(digest)
        sizes: dict[str, int] = {}
        for entry in artifact.entries:
            if entry.sha256 not in sizes:
                sizes[entry.sha256] = (
                    (destination / BLOBS_DIR / entry.sha256)
                    .stat(follow_symlinks=False)
                    .st_size
                )
        return PreservedPackage(
            digest=digest,
            root=destination,
            base_sha=artifact.base_sha,
            scope=artifact.scope,
            entry_count=len(artifact.entries),
            deletion_count=len(artifact.deletions),
            # Aggregated over ENTRIES, matching the reader's own bound: many
            # paths may share one blob, and the cost of applying the package is
            # measured in entries rather than in unique content.
            byte_length=sum(sizes[e.sha256] for e in artifact.entries),
        )

    def purge(self, digest: str) -> bool:
        """Remove a preserved package's bytes. True if there were any."""
        destination = self._packages / _hex_of(digest)
        if not destination.is_dir():
            return False
        for child in destination.rglob("*"):
            if child.is_file() and not child.is_symlink():
                child.chmod(0o600)
        shutil.rmtree(destination)
        _fsync_dir(self._packages)
        return True

    def _stage(self, artifact: Artifact, digest: str) -> Path:
        """Write the whole package into a discardable directory, flushed."""
        self._clear_stale(digest)
        staged = Path(tempfile.mkdtemp(dir=self._staging, prefix=f"{_hex_of(digest)}."))
        blobs = staged / BLOBS_DIR
        blobs.mkdir(mode=_DIR_MODE)
        written: set[str] = set()
        for entry in artifact.entries:
            if entry.sha256 in written:
                continue
            # Fetched by DIGEST, and re-hashed by the reader on the way out, so
            # the bytes preserved are the bytes that were verified.
            _write_exclusive(blobs / entry.sha256, artifact.content(entry))
            written.add(entry.sha256)
        _write_exclusive(staged / MANIFEST_NAME, _canonical_manifest(artifact))
        os.chmod(blobs, _DIR_MODE)  # noqa: S103,E501  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions
        os.chmod(staged, _DIR_MODE)  # noqa: S103,E501  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions
        _fsync_dir(blobs)
        _fsync_dir(staged)
        return staged

    def _clear_stale(self, digest: str) -> None:
        """Drop staging left by a crashed placement of this same package.

        A staging directory that was ever complete would have been renamed, so
        anything still under this digest's prefix is by definition unfinished
        and cannot be recovered from — only rebuilt, which is what happens next.
        """
        prefix = f"{_hex_of(digest)}."
        for child in self._staging.iterdir():
            if child.name.startswith(prefix):
                shutil.rmtree(child, ignore_errors=True)
