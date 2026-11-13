"""Extract a checkout as inert data for canonical publication.

Extraction happens in a **second container** over the finished checkout, rather
than in the agent's own run. That is deliberate:

* it is additive — the agent entrypoint contract does not change, so a failure
  here cannot damage a run that already succeeded;
* it is identical whichever runner produced the work, so publication does not
  quietly depend on which image was chosen;
* it happens after the agent has exited, so nothing is racing the tree it reads.

The host still never points git at the checkout. The container reads it (with
untrusted code already resident, so this grants an attacker nothing new) and the
host reads only the artifact — as opaque bytes, through
:func:`kew.publication_artifact.read_artifact`, which re-derives every digest.

The container gets no network or runner credentials, and the checkout read-only.
Every proxy variable is blanked: with no network a forwarded proxy value can
never be reached, and would only place its embedded credentials inside the
container. The dispatch's proxy policy still scopes redaction of what comes
back. See
:func:`kew.sandbox.build_extract_argv` for why each of those is narrower than
the agent's own container.

Native dispatches use :func:`extract_on_host`. That producer walks the checkout
with Python and never asks git to resolve inside it. It includes every unknown
regular file, with three exceptions, all for untracked paths only:

* it does not enter a real directory named ``node_modules`` or ``.venv`` when
  the base tracks nothing at or beneath it (#2099). Those are dependency
  installs (``npm ci``, ``uv sync``) whose symlinks would otherwise refuse
  every publication. A symlink or file with either name is walked as usual.
* it leaves out what the BASE commit's ``.gitignore`` files ignore (#2108),
  such as ``.svelte-kit/`` or a mkdocs ``site/``. The rules are read by the
  host from the base tree through *read_base_blob*, never from the checkout,
  so a ``.gitignore`` the agent wrote or edited changes nothing about what is
  left out; it publishes as an ordinary change. An ignored directory is not
  entered; an ignored file is not read. A tracked path is never left out,
  whatever the rules say. Matching is :mod:`kew.publication_ignore`.
* it does not descend into an untracked directory that is itself a git
  worktree or repository (#2201). An agent that ran ``git worktree add`` or
  ``git clone`` inside the workspace left another repository's checkout there:
  publication cannot carry it (its symlinks and gitlinks would refuse the
  whole package) and it is not the run's work. The test is git's own, not the
  NAME ``.git`` alone: a ``.git`` directory holding a ``HEAD`` and object and
  ref storage (or a ``commondir`` naming where those live), or a ``.git`` file
  reading ``gitdir: <path>``. A stray entry by that name — a fixture, a note,
  an empty directory, a symlink — leaves its parent published as before, which
  is what the base ref and the contained producer both do with it. The
  checkout's OWN ``.git`` is untouched by this — the walk already skips that
  name, and a directory the base holds, or tracks anything beneath, is walked
  as usual.

All three omissions happen before the mode check, so a symlink inside an
ignored, dependency or nested-repository directory does not refuse the
publication; a symlink or other non-regular entry that is not left out still
refuses it. Omitted paths are logged and named on the returned artifact. The
publication filter remains the authority for every emitted path, mode, and
payload. It validates every entry before applying its small, explicit
publication-scope exclusion for generated cache directories and untracked
dependency installs. The contained producer
(``docker/extract-artifact.sh``) lists untracked files with
``--exclude-standard``, so it applies the checkout's own ignore rules instead.

The two producers do NOT agree about a nested repository, and that is recorded
here rather than reconciled. ``git ls-files --others`` reports the nested
checkout as one untracked DIRECTORY, which is not a regular file, so the
contained producer refuses the WHOLE extraction and the run's other, correct
work is lost with it. Loosening that would mean teaching the contained producer
to step over a directory git itself named — weakening the rule that every
enumerated path is either carried or refuses the package, inside the one
producer that runs where the agent does. The agent prompts forbid the directory
outright instead (``_templates/*.md.j2``), which is the outcome both producers
share: work put there never reaches the branch. Neither producer lets it pass
for real work, and :mod:`kew.no_change_seal` refuses to seal a no-change over a
workspace whose only content the host walker stepped over.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import stat as stat_module
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING

from kew.publication_artifact import (
    BLOBS_DIR,
    MANIFEST_NAME,
    MAX_BLOB_BYTES,
    MAX_ENTRIES,
    MAX_TOTAL_BYTES,
    WORKSPACE_SCOPE,
    read_artifact,
)
from kew.publication_filter import (
    ALLOWED_MODES,
    PublicationRefused,
    Rejection,
    is_untracked_dependency_directory,
    tracked_path_keys,
)
from kew.publication_ignore import IgnoreRules, parse_gitignore
from kew.sandbox import (
    build_extract_argv,
    container_client_failure,
    proxy_redaction_scope,
    run_container_with_cpu_cap_retry,
)

_LOG = logging.getLogger(__name__)

if TYPE_CHECKING:
    from kew.publication_artifact import Artifact

__all__ = ["ExtractionFailed", "extract_in_container", "extract_on_host"]

#: The producer's exit codes (docker/extract-artifact.sh), so a failure says
#: what happened instead of "exit 7". Docker itself uses 125/126/127 for
#: launch failures, which is why the producer's own codes start at 2.
_PRODUCER_EXITS = {
    2: "the producer was called with the wrong arguments",
    3: "the image is missing git, jq or sha256sum",
    4: "the base commit is not in the agent's checkout",
    5: "a changed path has no entry in the tree",
    6: "the diff reported a status the producer does not accept",
    7: "the output directory was not empty",
    8: "the change contains a symlink or gitlink, which publication cannot carry",
    9: "the workspace could not be enumerated, so the package would be incomplete",
    10: "the workspace holds something that is not a regular file",
    11: "the output directory is inside the checkout the producer must capture",
    125: "the container runtime refused to start the container",
    126: "the extraction entrypoint is present but not executable",
    127: "the image has no extraction entrypoint — rebuild it",
}

#: Extraction is git plumbing over a finished checkout; it does not wait on a
#: model. A run that has not finished in this long is stuck, and the point of a
#: shadow comparison is that it never delays the publication it observes.
EXTRACT_TIMEOUT_SECONDS = 300

#: The host walker retains one path per entry until the manifest is written.
#: Bounding the entry count and the path bytes keeps an attacker-planted
#: directory of millions of empty files from exhausting host memory before the
#: artifact reader's own MAX_ENTRIES check could run (review round 3).
MAX_MANIFEST_PATH_BYTES = 8 * 1024 * 1024

#: Marker a digest table carries for a base blob above MAX_BLOB_BYTES: the
#: walker treats it as "changed" without ever reading the base bytes, so a
#: shrunk replacement of a huge file cannot trigger an unbounded read.
OVERSIZE_BASE_BLOB = "oversize:base-blob-exceeds-bound"

#: Marker a digest table carries for a base blob above MAX_BLOB_BYTES: the
#: walker treats it as "changed" without ever reading the base bytes, so a
#: shrunk replacement of a huge file cannot trigger an unbounded read.
OVERSIZE_BASE_BLOB = "oversize:base-blob-exceeds-bound"


#: The name git reserves for a repository's own administrative area. The walk
#: never publishes it, and it is what marks a nested worktree or clone (#2201).
_GIT_NAME = ".git"

#: Object and ref storage a repository directory has unless a ``commondir``
#: file says where they live instead (``is_git_directory``, setup.c).
_GIT_STORAGE_NAMES = frozenset({"objects", "refs"})

#: What a gitfile has to start with before git treats the path as a repository.
_GIT_FILE_PREFIX = b"gitdir: "

#: A gitfile holds one line naming one directory, so ``PATH_MAX`` bounds a real
#: one; this is that with room to spare. A larger file is not a gitfile and is
#: not read — the walk skips the name itself either way.
_GIT_FILE_MAX_BYTES = 8192


class ExtractionFailed(Exception):
    """The container did not produce a readable artifact.

    Always recoverable by the caller: in shadow mode the legacy path publishes
    regardless, and this is a divergence to record rather than an error to
    raise at an operator.
    """


def _manifest_mode(info: os.stat_result) -> str:
    """Return a stable git-like mode for one lstat result."""
    mode = info.st_mode
    if stat_module.S_ISREG(mode):
        return "100755" if mode & stat_module.S_IXUSR else "100644"
    if stat_module.S_ISLNK(mode):
        return "120000"
    if stat_module.S_ISDIR(mode):
        return "040000"
    return f"{mode & 0o177777:06o}"


def _read_regular(
    name: str,
    expected: os.stat_result,
    *,
    parent_fd: int,
    display_path: Path,
) -> bytes:
    """Read one bounded regular file relative to its retained directory."""
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=parent_fd)
    except OSError as exc:
        raise ExtractionFailed(f"could not open {display_path}: {exc}") from exc
    try:
        opened = _validate_opened_regular(display_path, expected, descriptor)
        return _read_descriptor(display_path, descriptor, opened.st_size)
    except OSError as exc:
        raise ExtractionFailed(f"could not read {display_path}: {exc}") from exc
    finally:
        os.close(descriptor)


def _validate_opened_regular(
    path: Path, expected: os.stat_result, descriptor: int
) -> os.stat_result:
    opened = os.fstat(descriptor)
    if not stat_module.S_ISREG(opened.st_mode):
        raise ExtractionFailed(f"refusing {path}: opened inode is not regular")
    if (opened.st_dev, opened.st_ino) != (expected.st_dev, expected.st_ino):
        raise ExtractionFailed(f"refusing {path}: inode changed during the walk")
    if opened.st_size != expected.st_size:
        raise ExtractionFailed(f"refusing {path}: size changed during the walk")
    if expected.st_nlink > 1 or opened.st_nlink > 1:
        raise ExtractionFailed(f"refusing {path}: regular file has multiple links")
    if opened.st_size > MAX_BLOB_BYTES:
        raise ExtractionFailed(
            f"refusing {path}: {opened.st_size} bytes exceeds {MAX_BLOB_BYTES}"
        )
    return opened


def _read_descriptor(path: Path, descriptor: int, expected_size: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while chunk := os.read(descriptor, min(1024 * 1024, MAX_BLOB_BYTES + 1 - size)):
        size += len(chunk)
        if size > MAX_BLOB_BYTES:
            raise ExtractionFailed(
                f"refusing {path}: content exceeds {MAX_BLOB_BYTES} bytes"
            )
        chunks.append(chunk)
    if os.fstat(descriptor).st_size != expected_size or size != expected_size:
        raise ExtractionFailed(f"refusing {path}: size changed while being read")
    return b"".join(chunks)


def _record_blob(blobs_dir: Path, content: bytes) -> str:
    digest = hashlib.sha256(content).hexdigest()
    target = blobs_dir / digest
    if not target.exists():
        target.write_bytes(content)
    return digest


def _is_inside(candidate: Path, root: Path) -> bool:
    resolved_candidate = candidate.resolve()
    resolved_root = root.resolve()
    return (
        resolved_candidate == resolved_root
        or resolved_root in resolved_candidate.parents
    )


def _inspect_at(parent_fd: int, name: str, display_path: Path) -> os.stat_result:
    try:
        return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as exc:
        raise ExtractionFailed(f"could not inspect {display_path}: {exc}") from exc


def _open_directory_at(
    parent_fd: int, name: str, expected: os.stat_result, display_path: Path
) -> int:
    """Open one child directory and bind descent to the inspected inode."""
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=parent_fd)
    except OSError as exc:
        raise ExtractionFailed(
            f"could not open directory {display_path}: {exc}"
        ) from exc
    try:
        opened = os.fstat(descriptor)
        if not stat_module.S_ISDIR(opened.st_mode):
            raise ExtractionFailed(f"refusing {display_path}: inode is not a directory")
        if (opened.st_dev, opened.st_ino) != (expected.st_dev, expected.st_ino):
            raise ExtractionFailed(
                f"refusing {display_path}: directory changed during the walk"
            )
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _open_checkout_directory(checkout_dir: Path, expected: os.stat_result) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(checkout_dir, flags)
    except OSError as exc:
        raise ExtractionFailed(
            f"could not open checkout {checkout_dir}: {exc}"
        ) from exc
    try:
        opened = os.fstat(descriptor)
        if not stat_module.S_ISDIR(opened.st_mode):
            raise ExtractionFailed("checkout is not a directory")
        if (opened.st_dev, opened.st_ino) != (expected.st_dev, expected.st_ino):
            raise ExtractionFailed("checkout directory changed before the walk")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


class _KnownBase:
    """What the trusted base already names, so only additions are bounded.

    Every traversed entry the base does not know, files and directories alike,
    counts towards ``MAX_ENTRIES``; an empty-directory fan-out is bounded the
    same way as a file fan-out (review round 4).
    """

    def __init__(
        self, base_state: Mapping[str, str], ignore_rules: IgnoreRules
    ) -> None:
        self._base_state = base_state
        self._ignore_rules = ignore_rules
        self._tracked_keys: frozenset[str] | None = None
        self._paths: frozenset[str] = frozenset(base_state)
        children: dict[str, set[str]] = {}
        for path in base_state:
            parts = path.split("/")
            for depth in range(len(parts)):
                prefix = "/".join(parts[:depth])
                children.setdefault(prefix, set()).add(parts[depth])
        self._children = {k: frozenset(v) for k, v in children.items()}
        self._directories = frozenset(children) - {""}
        self.added = 0

    def names_under(self, prefix: str) -> frozenset[str]:
        return self._children.get(prefix, frozenset())

    def knows(self, relative: str) -> bool:
        """True when the base holds this path, or tracks anything beneath it."""
        return relative in self._paths or relative in self._directories

    def is_untracked_install(self, relative: str) -> bool:
        """True for a dependency-install directory the base tracks nothing in.

        The exact-name test runs first, so the folded key set is built only
        for a walk that meets such a directory.
        """
        if self.knows(relative):
            return False
        if self._tracked_keys is None:
            if not is_untracked_dependency_directory(relative, frozenset()):
                return False
            self._tracked_keys = tracked_path_keys(self._base_state)
        return is_untracked_dependency_directory(relative, self._tracked_keys)

    def is_ignored_untracked(self, relative: str, *, is_dir: bool) -> bool:
        """True for an untracked path the base's ``.gitignore`` files ignore.

        A path the base tracks, or a directory it tracks anything beneath, is
        never ignored: git does not drop tracked content either.
        """
        if self.knows(relative):
            return False
        return self._ignore_rules.is_ignored(relative, is_dir=is_dir)

    def count(self, relative: str) -> None:
        if relative in self._paths or relative in self._directories:
            return
        self.added += 1
        if self.added > MAX_ENTRIES:
            raise ExtractionFailed(
                f"checkout adds more than {MAX_ENTRIES} entries the base does "
                "not know; refusing"
            )


@dataclass
class _Omissions:
    """What one walk left out on purpose, in walk order.

    Compared between the two walks like the entries, so a directory that
    appears or disappears between them is a changed checkout.
    """

    dependency_directories: list[str] = field(default_factory=list)
    ignored_paths: list[str] = field(default_factory=list)
    nested_repositories: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _WalkedEntry:
    relative: str
    display_path: Path
    info: os.stat_result
    content: bytes


def _directory_listing(
    directory_fd: int, display_path: Path, known: frozenset[str]
) -> list[str]:
    """List one directory, bounding the names the base does not know.

    Base paths are trusted and bounded by the base tree; only names the
    checkout added count towards ``MAX_ENTRIES``, so a legitimate large
    repository is not refused while an attacker-planted fan-out is.
    """
    names: list[str] = []
    added = 0
    try:
        with os.scandir(directory_fd) as listing:
            for item in listing:
                names.append(item.name)
                if item.name not in known:
                    added += 1
                    if added > MAX_ENTRIES:
                        raise ExtractionFailed(
                            f"refusing {display_path}: more than {MAX_ENTRIES} "
                            "entries the base does not know"
                        )
    except OSError as exc:
        raise ExtractionFailed(f"could not enumerate {display_path}: {exc}") from exc
    return sorted(names)


def _read_entry_content(
    parent_fd: int, name: str, info: os.stat_result, display_path: Path
) -> bytes:
    if stat_module.S_ISREG(info.st_mode):
        return _read_regular(name, info, parent_fd=parent_fd, display_path=display_path)
    if stat_module.S_ISLNK(info.st_mode):
        try:
            return os.fsencode(os.readlink(name, dir_fd=parent_fd))
        except OSError as exc:
            raise ExtractionFailed(
                f"could not read link {display_path}: {exc}"
            ) from exc
    return b""


def _holds_git_directory(
    parent_fd: int, info: os.stat_result, display_path: Path
) -> bool:
    """True when this ``.git`` DIRECTORY is one git would open as a repository.

    Git's own test (``is_git_directory``, setup.c): a ``HEAD``, plus object and
    ref storage — or a ``commondir`` file naming where that storage lives, as a
    ``--separate-git-dir`` checkout's admin directory has. A directory named
    ``.git`` that holds none of that is not a repository, whatever it is.
    """
    child_fd = _open_directory_at(parent_fd, _GIT_NAME, info, display_path)
    try:
        names = frozenset(os.listdir(child_fd))
    except OSError as exc:
        raise ExtractionFailed(f"could not enumerate {display_path}: {exc}") from exc
    finally:
        os.close(child_fd)
    if "HEAD" not in names:
        return False
    return "commondir" in names or _GIT_STORAGE_NAMES.issubset(names)


def _names_a_git_directory(
    parent_fd: int, info: os.stat_result, display_path: Path
) -> bool:
    """True when this ``.git`` FILE is a gitfile: ``gitdir: <path>``.

    Git's own syntax (``read_gitfile_gently``, setup.c) is the whole test. The
    target is deliberately NOT resolved: a linked worktree's admin directory
    lives under the main repository, outside the walked tree, and this producer
    reads nothing outside the checkout — least of all a path the agent wrote.
    So a gitfile whose target has gone stops the descent too; git's answer
    there is "not a repository" rather than "ordinary work", and what sits
    beside it is still another checkout.

    A gitfile holds one path, so a larger file is not one and is not read at
    all. The walk skips the name either way; reading it to decide would let a
    huge file that merely bears the name refuse an otherwise fine publication.
    """
    if info.st_size > _GIT_FILE_MAX_BYTES:
        return False
    content = _read_regular(
        _GIT_NAME, info, parent_fd=parent_fd, display_path=display_path
    )
    if not content.startswith(_GIT_FILE_PREFIX):
        return False
    return bool(content[len(_GIT_FILE_PREFIX) :].partition(b"\n")[0].strip())


def _is_nested_repository(
    directory_fd: int,
    display_path: Path,
    names: list[str],
    relative: str,
    *,
    known: _KnownBase,
) -> bool:
    """True when this directory below the root is a worktree or repository.

    Git's definition of one, not the NAME ``.git`` alone: ``git clone`` writes
    a ``.git`` directory holding a ``HEAD`` and object and ref storage, ``git
    worktree add`` a ``.git`` file reading ``gitdir: <path>`` (#2201). A stray
    entry by that name — a fixture, a note, an empty directory, a symlink the
    walk does not resolve — leaves its parent published exactly as before, as
    the base ref and the contained producer both still publish it.

    The checkout's own root never matches — *relative* is empty there — and a
    directory the base holds, or tracks anything beneath, is walked as usual:
    leaving it out would publish a deletion the agent never made.
    """
    if not relative or _GIT_NAME not in names or known.knows(relative):
        return False
    path = display_path / _GIT_NAME
    try:
        return _probe_git_entry(directory_fd, path)
    except ExtractionFailed as exc:
        # Unreadable is not evidence. The walk never publishes this name, so a
        # `.git` it cannot read is a repository it cannot prove — and refusing
        # the whole package over it would lose the run's real work elsewhere.
        # It says so rather than deciding quietly: this is the one answer here
        # reached without reading what it is about.
        _LOG.warning(
            "publication extraction could not read %s (%s), so %r is walked as "
            "ordinary work rather than a nested repository",
            path,
            exc,
            relative,
        )
        return False


def _probe_git_entry(parent_fd: int, display_path: Path) -> bool:
    """True when the ``.git`` entry here is one git would open."""
    info = _inspect_at(parent_fd, _GIT_NAME, display_path)
    if stat_module.S_ISDIR(info.st_mode):
        return _holds_git_directory(parent_fd, info, display_path)
    if stat_module.S_ISREG(info.st_mode):
        return _names_a_git_directory(parent_fd, info, display_path)
    # A symlink or anything else by that name: this producer resolves nothing,
    # so there is nothing here it can call a repository.
    return False


def _walk_directory(
    directory_fd: int,
    display_path: Path,
    relative_parts: tuple[str, ...],
    *,
    base_state: Mapping[str, str],
    present: set[str],
    omitted: _Omissions,
    known: _KnownBase,
) -> Iterator[_WalkedEntry]:
    prefix = "/".join(relative_parts)
    names = _directory_listing(directory_fd, display_path, known.names_under(prefix))
    # A nested worktree or clone is left out whole, with a recorded reason:
    # its symlinks and gitlinks would otherwise refuse the publication (#2201).
    if _is_nested_repository(directory_fd, display_path, names, prefix, known=known):
        omitted.nested_repositories.append(prefix)
        return
    for name in names:
        if name == _GIT_NAME:
            continue
        path = display_path / name
        parts = (*relative_parts, name)
        relative = "/".join(parts)
        known.count(relative)
        info = _inspect_at(directory_fd, name, path)
        # An untracked dependency install is not entered at all (#2099). The
        # lstat above decides "directory": a symlink or file with the reserved
        # name takes the ordinary path below and is refused or carried.
        is_dir = stat_module.S_ISDIR(info.st_mode)
        if is_dir and known.is_untracked_install(relative):
            omitted.dependency_directories.append(relative)
            continue
        # An untracked path the BASE's .gitignore files ignore is left out
        # before its mode is judged (#2108): an ignored directory is not
        # entered, an ignored file or symlink is not read. `is_dir` is lstat's,
        # so `site/` never matches a symlink named site, which refuses below.
        if known.is_ignored_untracked(relative, is_dir=is_dir):
            omitted.ignored_paths.append(relative)
            continue
        # Descend into every directory that is not a gitlink in the base. A
        # base FILE replaced by a directory is a deletion plus additions, as
        # the contained producer already reports it (review round 5).
        is_plain_directory = (
            stat_module.S_ISDIR(info.st_mode) and base_state.get(relative) != "160000"
        )
        if is_plain_directory:
            child_fd = _open_directory_at(directory_fd, name, info, path)
            try:
                yield from _walk_directory(
                    child_fd,
                    path,
                    parts,
                    base_state=base_state,
                    present=present,
                    omitted=omitted,
                    known=known,
                )
            finally:
                os.close(child_fd)
            continue
        present.add(relative)
        yield _WalkedEntry(
            relative=relative,
            display_path=path,
            info=info,
            content=_read_entry_content(directory_fd, name, info, path),
        )


def _walk_checkout(
    checkout_dir: Path,
    *,
    root_info: os.stat_result,
    base_state: Mapping[str, str],
    present: set[str],
    omitted: _Omissions,
    ignore_rules: IgnoreRules,
) -> Iterator[_WalkedEntry]:
    """Yield entries through retained directory descriptors only.

    Untracked dependency-install directories the walk did not enter, and
    untracked paths *ignore_rules* ignore, are recorded on *omitted*.
    """
    root_fd = _open_checkout_directory(checkout_dir, root_info)
    known = _KnownBase(base_state, ignore_rules)
    path_bytes = 0
    try:
        for entry in _walk_directory(
            root_fd,
            checkout_dir,
            (),
            base_state=base_state,
            present=present,
            omitted=omitted,
            known=known,
        ):
            path_bytes += len(os.fsencode(entry.relative))
            if path_bytes > MAX_MANIFEST_PATH_BYTES:
                raise ExtractionFailed(
                    f"checkout paths exceed {MAX_MANIFEST_PATH_BYTES} bytes; refusing"
                )
            yield entry
    finally:
        os.close(root_fd)


def _base_digest(
    path: str,
    read_base_blob: Callable[[str], bytes],
    base_digests: dict[str, str],
) -> str:
    """The base blob's sha256, from the shared table or one bounded read."""
    known = base_digests.get(path)
    if known is not None:
        # OVERSIZE_BASE_BLOB never equals a real digest: the entry reads as
        # changed and the base bytes are never requested.
        return known
    try:
        computed = hashlib.sha256(read_base_blob(path)).hexdigest()
    except Exception as exc:
        raise ExtractionFailed(f"could not read base blob for {path}: {exc}") from exc
    base_digests[path] = computed
    return computed


def _manifest_entry(
    walked: _WalkedEntry,
    *,
    blobs_dir: Path,
    base_state: Mapping[str, str],
    read_base_blob: Callable[[str], bytes],
    base_digests: dict[str, str],
) -> tuple[dict[str, str] | None, int]:
    path = walked.relative
    info = walked.info
    mode = _manifest_mode(info)
    if stat_module.S_ISDIR(info.st_mode) and base_state.get(path) == "160000":
        mode = "160000"
    if mode not in ALLOWED_MODES:
        raise PublicationRefused(
            (Rejection(path, "mode_not_allowed", f"mode {mode} is not a regular file"),)
        )
    content = walked.content
    digest = hashlib.sha256(content).hexdigest()
    if (
        base_state.get(path) == mode
        and mode in ALLOWED_MODES
        and _base_digest(path, read_base_blob, base_digests) == digest
    ):
        return None, 0
    _record_blob(blobs_dir, content)
    return {"path": path, "mode": mode, "sha256": digest}, len(content)


def _require_host_checkout(checkout_dir: Path, artifact_dir: Path) -> os.stat_result:
    try:
        root_info = checkout_dir.lstat()
    except OSError as exc:
        raise ExtractionFailed(f"checkout cannot be read: {exc}") from exc
    if not stat_module.S_ISDIR(root_info.st_mode):
        raise ExtractionFailed("checkout is not a directory")
    if artifact_dir.exists() or artifact_dir.is_symlink():
        raise FileExistsError(artifact_dir)
    if _is_inside(artifact_dir, checkout_dir):
        raise ExtractionFailed("artifact directory is inside the checkout")
    return root_info


def _render_artifact_entries(
    walked: Iterator[_WalkedEntry],
    *,
    blobs_dir: Path,
    base_state: Mapping[str, str],
    read_base_blob: Callable[[str], bytes],
    base_digests: dict[str, str],
) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    total = 0
    for item in walked:
        entry, size = _manifest_entry(
            item,
            blobs_dir=blobs_dir,
            base_state=base_state,
            read_base_blob=read_base_blob,
            base_digests=base_digests,
        )
        if entry is None:
            continue
        entries.append(entry)
        total += size
        if total > MAX_TOTAL_BYTES:
            raise ExtractionFailed(
                f"artifact expands past the {MAX_TOTAL_BYTES} byte limit"
            )
    return entries


def _refuse_unsafe_base_entries(
    *, base_state: Mapping[str, str], present: set[str]
) -> None:
    for path, mode in base_state.items():
        if path in present or mode in ALLOWED_MODES:
            continue
        raise PublicationRefused(
            (Rejection(path, "mode_not_allowed", f"mode {mode} is not a regular file"),)
        )


def _write_host_manifest(artifact_dir: Path, document: Mapping[str, object]) -> None:
    try:
        payload = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
        (artifact_dir / MANIFEST_NAME).write_text(payload, encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ExtractionFailed(f"could not write artifact manifest: {exc}") from exc


def _collect_workspace(
    checkout_dir: Path,
    *,
    root_info: os.stat_result,
    base_state: Mapping[str, str],
    read_base_blob: Callable[[str], bytes],
    blobs_dir: Path,
    base_digests: dict[str, str],
    ignore_rules: IgnoreRules,
) -> tuple[list[dict[str, str]], list[str], _Omissions]:
    """One full walk: manifest rows, implied deletions, and what it left out."""
    present: set[str] = set()
    omitted = _Omissions()
    walked = _walk_checkout(
        checkout_dir,
        root_info=root_info,
        base_state=base_state,
        present=present,
        omitted=omitted,
        ignore_rules=ignore_rules,
    )
    entries = _render_artifact_entries(
        walked,
        blobs_dir=blobs_dir,
        base_state=base_state,
        read_base_blob=read_base_blob,
        base_digests=base_digests,
    )
    # An uncarryable base entry must fail even when the checkout removed it.
    # Treating it as a plain deletion would bypass the mode gate.
    _refuse_unsafe_base_entries(base_state=base_state, present=present)
    deletions = sorted(
        path
        for path, mode in base_state.items()
        if path not in present and mode in ALLOWED_MODES
    )
    return sorted(entries, key=lambda row: row["path"]), deletions, omitted


def _base_ignore_rules(
    base_state: Mapping[str, str],
    read_base_blob: Callable[[str], bytes],
    base_digests: Mapping[str, str],
) -> IgnoreRules:
    """Parse every ``.gitignore`` the BASE tree holds, read from the base.

    The checkout's copies are never read: the agent cannot choose what is
    left out. A base ``.gitignore`` above the blob bound is skipped with a
    warning, which only means less is left out.
    """
    files = {}
    for path in IgnoreRules.gitignore_paths(base_state):
        if base_digests.get(path) == OVERSIZE_BASE_BLOB:
            _LOG.warning("base %s exceeds the blob bound; its rules are unused", path)
            continue
        try:
            content = read_base_blob(path)
        except Exception as exc:
            raise ExtractionFailed(
                f"could not read base ignore file {path}: {exc}"
            ) from exc
        if len(content) > MAX_BLOB_BYTES:
            _LOG.warning("base %s exceeds the blob bound; its rules are unused", path)
            continue
        files[path.rpartition("/")[0]] = parse_gitignore(content)
    return IgnoreRules(files)


def _log_omissions(omitted: _Omissions) -> None:
    if omitted.dependency_directories:
        count = len(omitted.dependency_directories)
        _LOG.info(
            "publication extraction skipped %d untracked dependency install "
            "director%s: %s",
            count,
            "y" if count == 1 else "ies",
            ", ".join(repr(path) for path in omitted.dependency_directories),
        )
    if omitted.ignored_paths:
        _LOG.info(
            "publication extraction left out %d untracked path%s the base "
            ".gitignore rules ignore: %s",
            len(omitted.ignored_paths),
            "" if len(omitted.ignored_paths) == 1 else "s",
            ", ".join(repr(path) for path in omitted.ignored_paths),
        )
    if omitted.nested_repositories:
        count = len(omitted.nested_repositories)
        _LOG.info(
            "publication extraction skipped %d nested git worktree or "
            "repository director%s: %s",
            count,
            "y" if count == 1 else "ies",
            ", ".join(repr(path) for path in omitted.nested_repositories),
        )


def extract_on_host(
    checkout_dir: Path,
    artifact_dir: Path,
    base_state: Mapping[str, str],
    read_base_blob: Callable[[str], bytes],
    base_sha: str,
    *,
    base_digests: Mapping[str, str] | None = None,
) -> Artifact:
    """Walk *checkout_dir* as data and return a verified workspace artifact.

    *base_digests* (path -> sha256 of the base blob) lets the walk compare
    without reading base bytes; paths it lacks fall back to *read_base_blob*
    once, and both walks share the table.

    Untracked dependency-install directories the walk skipped are logged and
    named on the result's ``omitted_dependency_directories``; untracked paths
    the base commit's ``.gitignore`` files ignore, on ``omitted_ignored_paths``;
    untracked directories that are themselves a git worktree or repository, on
    ``omitted_nested_repositories``.
    """
    digests: dict[str, str] = dict(base_digests or {})
    root_info = _require_host_checkout(checkout_dir, artifact_dir)
    ignore_rules = _base_ignore_rules(base_state, read_base_blob, digests)
    artifact_dir.mkdir(parents=True, mode=0o700)
    artifact_dir.chmod(0o700)
    blobs_dir = artifact_dir / BLOBS_DIR
    blobs_dir.mkdir(mode=0o700)
    first = _collect_workspace(
        checkout_dir,
        root_info=root_info,
        base_state=base_state,
        read_base_blob=read_base_blob,
        blobs_dir=blobs_dir,
        base_digests=digests,
        ignore_rules=ignore_rules,
    )
    # The agent has exited, but a process it left behind could still be
    # writing. Two identical walks are the stable snapshot the host can prove
    # without stopping the world; a difference is refused (review round 4).
    second = _collect_workspace(
        checkout_dir,
        root_info=root_info,
        base_state=base_state,
        read_base_blob=read_base_blob,
        blobs_dir=blobs_dir,
        base_digests=digests,
        ignore_rules=ignore_rules,
    )
    if first != second:
        raise ExtractionFailed(
            "the checkout changed while it was being read; something is still "
            "writing to it"
        )
    entries, deletions, omitted = first
    document = {
        "scope": WORKSPACE_SCOPE,
        "base": base_sha,
        "entries": entries,
        "deletions": deletions,
    }
    _write_host_manifest(artifact_dir, document)
    _log_omissions(omitted)
    return replace(
        read_artifact(artifact_dir),
        omitted_dependency_directories=tuple(omitted.dependency_directories),
        omitted_ignored_paths=tuple(omitted.ignored_paths),
        omitted_nested_repositories=tuple(omitted.nested_repositories),
    )


def extract_in_container(
    *,
    runtime: str,
    image: str,
    checkout_dir: Path,
    artifact_dir: Path,
    slug: str,
    base_sha: str,
    proxy_environment: str,
    timeout: float = EXTRACT_TIMEOUT_SECONDS,
) -> Artifact:
    """Extract *checkout_dir*'s work into *artifact_dir*, and read it back.

    *artifact_dir* must not exist. It is created here, mode 0700 and owned by
    this process, because a bind mount target that docker creates is owned by
    root — and because the producer refuses a directory that already has
    content, which is the check that stops a re-run from leaving blobs the new
    manifest does not reference.

    *base_sha* must be a full hex object id the AGENT's checkout contains.
    Resolve it from the canonical repository, never from the checkout: asking
    the agent's own repo what its base is means accepting its answer.
    """
    # Built BEFORE anything is created: it validates the base, and a rejected
    # base that had already made the directory would leave the corrected retry
    # failing on "already exists" — one bad call poisoning the good one.
    argv = build_extract_argv(
        runtime=runtime,
        image=image,
        checkout_dir=checkout_dir,
        artifact_dir=artifact_dir,
        slug=slug,
        base_sha=base_sha,
        uid=os.getuid(),
        gid=os.getgid(),
    )
    artifact_dir.mkdir(mode=0o700, parents=True)
    # mkdir's mode is masked by the umask — under 0o277 it yields 0500, which
    # this process cannot write the artifact into. Set it explicitly so the
    # mode is ours rather than ambient process state's.
    artifact_dir.chmod(0o700)
    with proxy_redaction_scope(proxy_environment):
        result = run_container_with_cpu_cap_retry(argv, timeout=timeout)
        if (failure := container_client_failure(result)) is not None:
            raise ExtractionFailed(failure)
        if result.returncode != 0:
            detail = _PRODUCER_EXITS.get(result.returncode, f"exit {result.returncode}")
            stderr = (result.stderr or "").strip()
            raise ExtractionFailed(f"{detail}{f': {stderr}' if stderr else ''}")
        return read_artifact(artifact_dir)
