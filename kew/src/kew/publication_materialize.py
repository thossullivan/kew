"""Write a scanned git tree into a working tree as the exact blob bytes.

Work a remote instance returns is scanned as a git tree
(:func:`kew.dispatch_placement._scan_remote_work`), and the operator's
checkout must then hold exactly the bytes that scan read. Git's own checkout
machinery cannot promise that: ``read-tree -u`` and ``checkout-index`` apply
whatever the tree's ``.gitattributes`` select — a smudge filter, ``eol=crlf``,
``ident``, ``working-tree-encoding`` — and writing an index fires a
repository's ``post-index-change`` hook. Each of those is a program or a
conversion that runs AFTER the scan, so the bytes on disk would be ones the
scan never saw (#2569 review).

So nothing here checks anything out. The blobs that differ from the base are
read raw with one ``git cat-file --batch`` (no ``--filters``, so no attribute
applies) and streamed to disk with plain file calls, one bounded chunk at a
time, so memory never holds more than a chunk of any blob: the executable bit
and symlinks are kept as the tree records them, and nothing else about a path
is interpreted. The operator checkout's index is never written; the
publication that follows reads the working tree raw, exactly as it does after
a local agent.

The invariant is the WHOLE tree, not only the changed paths: every file the
scanned tree records is on disk as exactly its blob. A path the artifact left
alone still holds whatever the base checkout wrote, and a base ``*.txt
eol=crlf`` made those bytes something other than the blob — which the raw
publication read would then publish as a change nobody scanned. So every
unchanged path is hashed as it lies on disk, and one that differs is rewritten
from its blob like a change (#2591).

Names are checked as a SET before anything is written: ``README.md`` in the
base and ``readme.md`` from the artifact are two git paths but one file on a
case-insensitive, normalising volume, and writing both would leave one entry
silently missing. The publication filter's collision rule runs over the base's
paths and the whole difference, and any collision refuses (#2591).

Every path is re-checked here rather than trusted from upstream: it must pass
the publication filter's shape rules (no traversal, no ``.git`` component),
and every directory on the way to it is opened with ``O_NOFOLLOW`` relative to
its parent, so a symlinked ancestor — tracked by the base or planted in the
checkout — is refused rather than followed out of the tree.
"""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import tempfile
from contextlib import ExitStack
from dataclasses import dataclass
from typing import IO, TYPE_CHECKING

from kew.publication_apply import inert_git_env
from kew.publication_filter import collision_refusals, path_shape_refusal
from kew.subprocess_utils import popen_bytes, run_bytes

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

__all__ = ["MaterializeRefused", "write_tree_bytes"]

_MODE_FILE = "100644"
_MODE_EXECUTABLE = "100755"
_MODE_SYMLINK = "120000"
_WRITABLE_MODES = frozenset({_MODE_FILE, _MODE_EXECUTABLE, _MODE_SYMLINK})

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_CREATE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC

#: The most of one blob held in memory at once, whether written or hashed.
_CHUNK = 1 << 20
#: A symlink target is held whole to create the link; no filesystem accepts one
#: anywhere near this long, so a larger blob is refused rather than buffered.
_MAX_SYMLINK_TARGET = 1 << 16


class MaterializeRefused(Exception):
    """The scanned tree could not be written byte-for-byte, so nothing more is."""


@dataclass(frozen=True)
class _Change:
    """One path that differs between the base tree and the scanned one."""

    path: str
    #: The scanned tree's mode and blob, or ``None`` for a removal.
    mode: str | None
    oid: str | None


def write_tree_bytes(checkout_dir: Path, *, base_sha: str, tree: str) -> None:
    """Make every file *tree* records in *checkout_dir* exactly its blob.

    Removals go first, so a path the tree turns from a directory into a file
    (or back) has its old shape gone before the new one is written; a write
    replaces the file or symlink already at its path. Untracked files the tree
    does not name are left alone.
    """
    changes = _changes(checkout_dir, base_sha=base_sha, tree=tree)
    for change in changes:
        _check(change)
    _check_collisions(checkout_dir, base_sha=base_sha, changes=changes)
    root = os.open(checkout_dir, _DIR_FLAGS)
    try:
        writes = [change for change in changes if change.mode is not None]
        writes += _drifted(checkout_dir, root, tree=tree, changes=changes)
        for change in changes:
            if change.mode is None:
                _remove(root, change.path)
        _write_blobs(checkout_dir, root, writes)
    finally:
        os.close(root)


def _check(change: _Change) -> None:
    """Refuse a path the filter would, or a mode that is not a file or link."""
    refusal = path_shape_refusal(change.path)
    if refusal is not None:
        raise MaterializeRefused(f"{change.path!r}: {refusal.detail}")
    if change.mode is not None and change.mode not in _WRITABLE_MODES:
        raise MaterializeRefused(f"{change.path!r} has mode {change.mode}")


def _check_collisions(
    checkout_dir: Path, *, base_sha: str, changes: list[_Change]
) -> None:
    """Refuse a difference whose names a folding filesystem would merge."""
    base_paths = [
        name.decode("utf-8", errors="surrogateescape")
        for name in _git(
            checkout_dir, "ls-tree", "-r", "-z", "--name-only", f"{base_sha}^{{tree}}"
        ).split(b"\0")
        if name
    ]
    operations = [(change.path, change.mode is not None) for change in changes]
    refusals = collision_refusals(operations, base_paths)
    if refusals:
        first = refusals[0]
        raise MaterializeRefused(f"{first.path!r} {first.detail}")


def _git(checkout_dir: Path, *args: str, stdin: bytes | None = None) -> bytes:
    """Run git under :func:`~kew.publication_apply.inert_git_env`."""
    result = run_bytes(
        ["git", "-C", str(checkout_dir), *args], stdin=stdin, env=inert_git_env()
    )
    if result.returncode != 0:
        detail = (result.stderr or b"").decode("utf-8", errors="replace").strip()
        raise MaterializeRefused(f"git {args[0]} failed: {detail}")
    return result.stdout or b""


def _changes(checkout_dir: Path, *, base_sha: str, tree: str) -> list[_Change]:
    """Every path whose blob or mode differs between the base and *tree*.

    Raw ``diff-tree`` output: object ids and modes only, never content, so no
    diff driver or textconv is consulted.
    """
    raw = _git(
        checkout_dir,
        "diff-tree",
        "-r",
        "-z",
        "--no-renames",
        "--no-ext-diff",
        "--no-textconv",
        f"{base_sha}^{{tree}}",
        tree,
    )
    fields = raw.split(b"\0")
    changes: list[_Change] = []
    for meta, name in zip(fields[0::2], fields[1::2], strict=False):
        try:
            parts = meta.decode("ascii", errors="strict").lstrip(":").split()
            path = name.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise MaterializeRefused("the tree difference is not text") from exc
        if len(parts) != 5:
            raise MaterializeRefused("the tree difference is malformed")
        _, new_mode, _, new_oid, status = parts
        if status == "D":
            changes.append(_Change(path, None, None))
        elif status in {"A", "M", "T"}:
            changes.append(_Change(path, new_mode, new_oid))
        else:
            raise MaterializeRefused(f"{path!r} changed as {status!r}")
    return changes


def _tree_entries(checkout_dir: Path, tree: str) -> Iterator[_Change]:
    """Every entry *tree* records, recursively, as ``(path, mode, oid)``."""
    for record in _git(checkout_dir, "ls-tree", "-r", "-z", tree).split(b"\0"):
        if not record:
            continue
        meta, _, name = record.partition(b"\t")
        parts = meta.decode("ascii", errors="replace").split()
        if len(parts) != 3:
            raise MaterializeRefused("the tree listing is malformed")
        try:
            path = name.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise MaterializeRefused("the tree listing is not text") from exc
        yield _Change(path, parts[0], parts[2])


def _drifted(
    checkout_dir: Path, root: int, *, tree: str, changes: list[_Change]
) -> list[_Change]:
    """The unchanged paths whose bytes or shape on disk are not their blob's.

    The base checkout wrote these through the base's attributes, so an
    ``eol=crlf`` or ``ident`` path holds converted bytes; each is rewritten from
    its blob so the whole tree, not only the difference, is what was scanned.
    """
    algorithm = _git(checkout_dir, "rev-parse", "--show-object-format").decode().strip()
    changed = {change.path for change in changes}
    drifted: list[_Change] = []
    for entry in _tree_entries(checkout_dir, tree):
        mode = entry.mode
        if entry.path in changed or mode is None or mode not in _WRITABLE_MODES:
            continue
        if _disk_oid(root, entry.path, mode, algorithm) != entry.oid:
            _check(entry)
            drifted.append(entry)
    return drifted


def _disk_oid(root: int, path: str, mode: str, algorithm: str) -> str | None:
    """The blob id of *path*'s raw bytes, or ``None`` when its shape is not *mode*."""
    name = path.rsplit("/", 1)[-1]
    try:
        for parent in _parents(root, path, create=False):
            found = os.lstat(name, dir_fd=parent)
            if mode == _MODE_SYMLINK:
                if not stat.S_ISLNK(found.st_mode):
                    return None
                return _blob_id(
                    algorithm, os.fsencode(os.readlink(name, dir_fd=parent))
                )
            executable = bool(found.st_mode & stat.S_IXUSR)
            if not stat.S_ISREG(found.st_mode) or executable != (
                mode == _MODE_EXECUTABLE
            ):
                return None
            fd = os.open(name, _READ_FLAGS, dir_fd=parent)
            with os.fdopen(fd, "rb") as handle:
                return _stream_blob_id(algorithm, handle, os.fstat(fd).st_size)
    except (OSError, MaterializeRefused):
        # Missing, or behind something that is not a plain directory: either
        # way not the blob, and the rewrite is what refuses or repairs it.
        return None
    return None


def _blob_id(algorithm: str, data: bytes) -> str:
    digest = hashlib.new(algorithm, f"blob {len(data)}\0".encode())
    digest.update(data)
    return digest.hexdigest()


def _stream_blob_id(algorithm: str, handle: IO[bytes], size: int) -> str:
    digest = hashlib.new(algorithm, f"blob {size}\0".encode())
    while chunk := handle.read(_CHUNK):
        digest.update(chunk)
    return digest.hexdigest()


def _write_blobs(checkout_dir: Path, root: int, writes: list[_Change]) -> None:
    """Stream each write's raw blob from one ``cat-file --batch`` to its path.

    The requests come from a file and errors go to one, so neither pipe can
    fill while this reads the other; the only pipe is the blob stream, read a
    bounded chunk at a time straight into the destination file.
    """
    if not writes:
        return
    with tempfile.TemporaryFile() as requests, tempfile.TemporaryFile() as errors:
        requests.write("".join(f"{change.oid}\n" for change in writes).encode())
        requests.seek(0)
        process = popen_bytes(
            ["git", "-C", str(checkout_dir), "cat-file", "--batch"],
            stdin=requests.fileno(),
            stdout=subprocess.PIPE,
            stderr=errors.fileno(),
            env=inert_git_env(),
        )
        stream = process.stdout
        try:
            if stream is None:
                raise MaterializeRefused("git cat-file has no output pipe")
            for change in writes:
                _stream_one(root, change, stream)
            if stream.read(1):
                raise MaterializeRefused("git cat-file wrote past the last blob")
            returncode = process.wait()
        finally:
            # Only an abandoned stream leaves git running; a finished one has
            # reached EOF above and is waited for, never killed mid-exit.
            if process.poll() is None:
                process.kill()
                process.wait()
            if stream is not None:
                stream.close()
        if returncode != 0:
            errors.seek(0)
            detail = errors.read().decode("utf-8", errors="replace").strip()
            raise MaterializeRefused(f"git cat-file failed: {detail}")


def _stream_one(root: int, change: _Change, stream: IO[bytes]) -> None:
    """Read one batch record for *change* off *stream* and write it to disk."""
    if change.oid is None or change.mode is None:
        raise MaterializeRefused(f"{change.path!r} has no blob to write")
    header = stream.readline(256).decode("ascii", errors="replace").split()
    if (
        len(header) != 3
        or header[0] != change.oid
        or header[1] != "blob"
        or not header[2].isdigit()
    ):
        raise MaterializeRefused(f"object {change.oid} is not a readable blob")
    _write(root, change.path, change.mode, stream, int(header[2]))
    if stream.read(1) != b"\n":
        raise MaterializeRefused(f"object {change.oid} was read short")


def _read_exact(stream: IO[bytes], size: int) -> Iterator[bytes]:
    """*size* bytes off *stream*, a bounded chunk at a time."""
    remaining = size
    while remaining:
        chunk = stream.read(min(_CHUNK, remaining))
        if not chunk:
            raise MaterializeRefused("a blob was read short")
        remaining -= len(chunk)
        yield chunk


def _parents(root: int, path: str, *, create: bool) -> Iterator[int]:
    """Open each directory above *path* without following a symlink.

    Yields the innermost one's descriptor, or nothing when a directory is
    missing and *create* is false (so there is nothing to remove).
    """
    components = path.split("/")[:-1]
    with ExitStack() as stack:
        current = root
        for component in components:
            try:
                fd = os.open(component, _DIR_FLAGS, dir_fd=current)
            except FileNotFoundError:
                if not create:
                    return
                os.mkdir(component, 0o777, dir_fd=current)
                fd = os.open(component, _DIR_FLAGS, dir_fd=current)
            except NotADirectoryError as exc:
                raise MaterializeRefused(
                    f"{path!r} passes through {component!r}, which is a file "
                    "or a symlink in the checkout"
                ) from exc
            except OSError as exc:
                # ELOOP: O_NOFOLLOW met a symlink where a directory belongs.
                raise MaterializeRefused(
                    f"{path!r} passes through {component!r}, which is not a "
                    "plain directory in the checkout"
                ) from exc
            stack.callback(os.close, fd)
            current = fd
        yield current


def _remove(root: int, path: str) -> None:
    """Remove *path*'s current file or symlink, if any, and prune empty parents."""
    name = path.rsplit("/", 1)[-1]
    for parent in _parents(root, path, create=False):
        try:
            found = os.lstat(name, dir_fd=parent)
        except FileNotFoundError:
            return
        if stat.S_ISDIR(found.st_mode):
            raise MaterializeRefused(f"{path!r} is a directory in the checkout")
        os.unlink(name, dir_fd=parent)
    _prune(root, path)


def _prune(root: int, path: str) -> None:
    """Remove the directories above *path* that its removal left empty."""
    components = path.split("/")[:-1]
    for depth in range(len(components), 0, -1):
        directory = "/".join(components[:depth])
        for parent in _parents(root, directory, create=False):
            try:
                os.rmdir(components[depth - 1], dir_fd=parent)
            except OSError:
                return


def _write(root: int, path: str, mode: str, stream: IO[bytes], size: int) -> None:
    """Write *size* bytes of *stream* at *path* as the tree's *mode* records it."""
    if mode == _MODE_SYMLINK and size > _MAX_SYMLINK_TARGET:
        raise MaterializeRefused(f"{path!r} is a {size}-byte symlink target")
    name = path.rsplit("/", 1)[-1]
    for parent in _parents(root, path, create=True):
        try:
            found = os.lstat(name, dir_fd=parent)
        except FileNotFoundError:
            found = None
        if found is not None:
            if stat.S_ISDIR(found.st_mode):
                raise MaterializeRefused(f"{path!r} is a directory in the checkout")
            os.unlink(name, dir_fd=parent)
        if mode == _MODE_SYMLINK:
            os.symlink(b"".join(_read_exact(stream, size)), name, dir_fd=parent)
            return
        # 0o777/0o666 under the process umask: the executable bit is the only
        # permission a tree records, which is what git itself writes.
        permissions = 0o777 if mode == _MODE_EXECUTABLE else 0o666
        fd = os.open(name, _CREATE_FLAGS, permissions, dir_fd=parent)
        with os.fdopen(fd, "wb") as handle:
            for chunk in _read_exact(stream, size):
                handle.write(chunk)
