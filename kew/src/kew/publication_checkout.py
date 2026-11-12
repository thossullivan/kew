"""The checkout escape hatch's commit and filter, held to canonical's terms (#2634).

Checkout publication (``publish = "checkout"``) commits inside the agent's own
checkout and pushes that commit. It used to do so with a plain ``git add -A`` /
``git commit`` under the operator's environment, so the checkout's own
configuration chose what ran on the host: a ``core.hooksPath`` pre-commit hook,
a ``core.fsmonitor`` program, a ``.gitattributes`` clean filter during
``add``, a ``gpg.program`` under ``commit.gpgSign`` — any of which could also
stage files after the agent finished. The commit then reached the remote after
only a secret scan, never the publication filter canonical publication runs.

So the commit here is built from plumbing that runs no program the checkout
names, under :func:`~kew.publication_apply.inert_git_env` (no hook, no
fsmonitor, no system or global config file, no attributes file):

* the index is reset to ``HEAD`` with ``read-tree``, and the working tree is
  read back with ``ls-files`` — the tracked set and the untracked,
  not-ignored set, which is what ``add -A`` records;
* every regular file is hashed with ``hash-object --no-filters``, and a
  symlink's target is hashed as its blob, so no attribute selects a
  conversion or a filter driver;
* the tree is written, committed with ``commit-tree`` under kew's fixed
  identity (never one the checkout's config chooses), and ``HEAD`` moved with
  ``update-ref``.

And before anything is pushed, :func:`filter_checkout_range` runs the same
:func:`~kew.publication_filter.filter_manifest` over the whole range the push
sends — commits the agent made itself included — so a symlink, a gitlink, a
mode escalation or a ``.gitattributes`` driver is refused, and a protected
path holds for a person, exactly as it would canonically.
"""

from __future__ import annotations

import hashlib
import os
import stat
from typing import TYPE_CHECKING

from kew.progress import _warn
from kew.publication import PublicationError, PublicationStep
from kew.publication_apply import inert_git_env
from kew.publication_canonical import PublicationHeld, host_commit_identity
from kew.publication_filter import (
    ManifestEntry,
    filter_manifest,
    protected_path_reason,
)
from kew.subprocess_utils import run_bytes

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

__all__ = ["commit_checkout_worktree", "filter_checkout_range"]

_MODE_FILE = "100644"
_MODE_EXECUTABLE = "100755"
_MODE_SYMLINK = "120000"
_MODE_GITLINK = "160000"
_OID_WIDTH = {"sha1": 40, "sha256": 64}


def _git(
    checkout_dir: Path,
    *args: str,
    env: dict[str, str],
    stdin: bytes | None = None,
    step: PublicationStep,
) -> bytes:
    result = run_bytes(
        ["git", "-C", str(checkout_dir), *args], stdin=stdin, env=env, timeout=300
    )
    if result.returncode != 0:
        detail = (result.stderr or b"").decode("utf-8", errors="replace").strip()
        raise PublicationError(step, f"git {args[0]} failed: {detail[:300]}")
    return result.stdout or b""


def _commit_env() -> dict[str, str]:
    """Inert git, signing off, and an identity the checkout cannot choose.

    The operator's own ``GIT_AUTHOR_*`` / ``GIT_COMMITTER_*`` still name the
    commit — the host environment is not the checkout's to write — and kew's
    fixed identity fills whatever they leave unset, since no config file
    is read to supply one.
    """
    identity = host_commit_identity()
    fallback = {"NAME": identity.name, "EMAIL": identity.email}
    env = inert_git_env(
        **{
            f"GIT_{role}_{part}": os.environ.get(f"GIT_{role}_{part}") or value
            for role in ("AUTHOR", "COMMITTER")
            for part, value in fallback.items()
        }
    )
    # A signing program is a program the checkout's config names.
    count = int(env["GIT_CONFIG_COUNT"])
    env[f"GIT_CONFIG_KEY_{count}"] = "commit.gpgSign"
    env[f"GIT_CONFIG_VALUE_{count}"] = "false"
    env["GIT_CONFIG_COUNT"] = str(count + 1)
    return env


def _split_z(output: bytes) -> list[str]:
    return [os.fsdecode(item) for item in output.split(b"\0") if item]


def _tracked_modes(checkout_dir: Path, env: dict[str, str]) -> dict[str, str]:
    """``path -> mode`` for every entry of the (just reset) index."""
    modes: dict[str, str] = {}
    for record in _split_z(
        _git(checkout_dir, "ls-files", "-s", "-z", env=env, step="commit")
    ):
        meta, _, path = record.partition("\t")
        modes[path] = meta.split(" ", 1)[0]
    return modes


def _hash_blob(checkout_dir: Path, env: dict[str, str], data: bytes) -> str:
    out = _git(
        checkout_dir, "hash-object", "-w", "--stdin", env=env, stdin=data, step="commit"
    )
    return out.decode("ascii").strip()


def _hash_files(
    checkout_dir: Path, env: dict[str, str], paths: list[str]
) -> dict[str, str]:
    """Raw blob ids for regular files, read as bytes with no filter applied."""
    if not paths:
        return {}
    if any("\n" in path for path in paths):
        raise PublicationError("commit", "a path holds a newline; refusing to hash it")
    out = _git(
        checkout_dir,
        "hash-object",
        "-w",
        "--no-filters",
        "--stdin-paths",
        env=env,
        stdin=b"".join(os.fsencode(path) + b"\n" for path in paths),
        step="commit",
    )
    oids = out.decode("ascii").split()
    if len(oids) != len(paths):
        raise PublicationError("commit", "hash-object answered the wrong count")
    return dict(zip(paths, oids, strict=True))


def _record(mode: str, oid: str, path: str) -> bytes:
    return os.fsencode(f"{mode} {oid}\t{path}") + b"\0"


def _working_mode(checkout_dir: Path, path: str, tracked: dict[str, str]) -> str | None:
    """The mode *path* holds on disk, ``"0"`` for gone, ``None`` for untouched."""
    if path.endswith("/"):
        raise PublicationError(
            "commit", f"{path!r} is a nested repository; refusing to embed it"
        )
    if tracked.get(path) == _MODE_GITLINK:
        return None
    try:
        info = os.lstat(checkout_dir / path)
    except (FileNotFoundError, NotADirectoryError):
        return "0" if path in tracked else None
    if stat.S_ISDIR(info.st_mode):
        return "0" if path in tracked else None
    if stat.S_ISLNK(info.st_mode):
        return _MODE_SYMLINK
    if stat.S_ISREG(info.st_mode):
        return _MODE_EXECUTABLE if info.st_mode & stat.S_IXUSR else _MODE_FILE
    raise PublicationError("commit", f"{path!r} is not a file or a symlink")


def _index_records(
    checkout_dir: Path,
    env: dict[str, str],
    tracked: dict[str, str],
    untracked: list[str],
    null_oid: str,
) -> list[bytes]:
    """``update-index --index-info`` records making the index the working tree."""
    records: list[bytes] = []
    regular: dict[str, str] = {}
    for path in sorted(set(tracked) | set(untracked)):
        mode = _working_mode(checkout_dir, path, tracked)
        if mode == "0":
            records.append(_record(mode, null_oid, path))
        elif mode == _MODE_SYMLINK:
            target = os.fsencode(os.readlink(checkout_dir / path))
            records.append(_record(mode, _hash_blob(checkout_dir, env, target), path))
        elif mode is not None:
            regular[path] = mode
    for path, oid in _hash_files(checkout_dir, env, list(regular)).items():
        records.append(_record(regular[path], oid, path))
    return records


def _out(
    checkout_dir: Path, *args: str, env: dict[str, str], stdin: bytes | None = None
) -> str:
    return (
        _git(checkout_dir, *args, env=env, stdin=stdin, step="commit")
        .decode("ascii")
        .strip()
    )


def _head(checkout_dir: Path, env: dict[str, str]) -> str | None:
    """``HEAD``'s commit, or ``None`` on an unborn branch."""
    result = run_bytes(
        ["git", "-C", str(checkout_dir), "rev-parse", "--verify", "--quiet"]
        + ["HEAD^{commit}"],
        env=env,
        timeout=300,
    )
    return result.stdout.decode("ascii").strip() if result.returncode == 0 else None


def _stage_working_tree(
    checkout_dir: Path, env: dict[str, str], head: str | None, null_oid: str
) -> str:
    """Make the index the working tree, as ``add -A`` would; return its tree."""
    _git(checkout_dir, "read-tree", head or "--empty", env=env, step="commit")
    tracked = _tracked_modes(checkout_dir, env)
    untracked = _split_z(
        _git(
            checkout_dir,
            "ls-files",
            "-z",
            "--others",
            "--exclude-standard",
            env=env,
            step="commit",
        )
    )
    records = _index_records(checkout_dir, env, tracked, untracked, null_oid)
    if records:
        _git(
            checkout_dir,
            "update-index",
            "-z",
            "--index-info",
            env=env,
            stdin=b"".join(records),
            step="commit",
        )
    return _out(checkout_dir, "write-tree", env=env)


def commit_checkout_worktree(checkout_dir: Path, message: str) -> bool:
    """Commit the working tree as ``add -A`` would, running nothing it names.

    Returns whether a commit was made: ``False`` when the working tree already
    is ``HEAD``'s tree. ``HEAD`` moves only from the commit read here, so a
    concurrent move refuses rather than being overwritten.
    """
    env = _commit_env()
    head = _head(checkout_dir, env)
    width = _OID_WIDTH.get(
        _out(checkout_dir, "rev-parse", "--show-object-format", env=env)
    )
    if width is None:
        raise PublicationError("commit", "unknown object format")
    null_oid = "0" * width
    tree = _stage_working_tree(checkout_dir, env, head, null_oid)
    if head is not None:
        before = _out(checkout_dir, "rev-parse", f"{head}^{{tree}}", env=env)
    else:
        before = _out(
            checkout_dir, "hash-object", "-t", "tree", "--stdin", env=env, stdin=b""
        )
    if tree == before:
        return False
    parents = ("-p", head) if head is not None else ()
    commit = _out(
        checkout_dir,
        "commit-tree",
        tree,
        *parents,
        "-F",
        "-",
        env=env,
        stdin=message.encode("utf-8"),
    )
    _git(
        checkout_dir,
        "update-ref",
        "HEAD",
        commit,
        head or null_oid,
        env=env,
        step="commit",
    )
    return True


def _range_manifest(
    checkout_dir: Path, *, base_sha: str, head_sha: str, env: dict[str, str]
) -> tuple[list[ManifestEntry], list[str], dict[str, str]]:
    """Writes, deletions and new blob ids between two trees, read raw."""
    raw = _git(
        checkout_dir,
        "diff-tree",
        "-r",
        "-z",
        "--no-renames",
        "--no-commit-id",
        base_sha,
        head_sha,
        env=env,
        step="push",
    ).split(b"\0")
    entries: list[ManifestEntry] = []
    deletions: list[str] = []
    oids: dict[str, str] = {}
    for meta, path_bytes in zip(raw[0::2], raw[1::2], strict=False):
        if not meta.startswith(b":"):
            continue
        _old_mode, new_mode, _old_oid, new_oid, status = (
            meta[1:].decode("ascii").split(" ")
        )
        path = os.fsdecode(path_bytes)
        if status == "D":
            deletions.append(path)
            continue
        entries.append(ManifestEntry(path=path, mode=new_mode))
        oids[path] = new_oid
    return entries, deletions, oids


def _base_modes(
    checkout_dir: Path, base_sha: str, env: dict[str, str]
) -> dict[str, str]:
    modes: dict[str, str] = {}
    listing = _git(
        checkout_dir,
        "ls-tree",
        "-r",
        "-z",
        "--full-tree",
        base_sha,
        env=env,
        step="push",
    )
    for record in _split_z(listing):
        meta, _, path = record.partition("\t")
        modes[path] = meta.split(" ", 1)[0]
    return modes


def _read_blob(checkout_dir: Path, oid: str, env: dict[str, str]) -> bytes:
    return _git(checkout_dir, "cat-file", "blob", oid, env=env, step="push")


def _with_digests(
    checkout_dir: Path,
    entries: list[ManifestEntry],
    oids: dict[str, str],
    env: dict[str, str],
) -> list[ManifestEntry]:
    """*entries*, a protected path's carrying its content's sha256.

    Only a protected write is ever compared with the merged base, so only its
    blob is read; every other entry keeps an empty digest, which the filter
    never exempts. A gitlink has no blob to read and is refused regardless.
    """
    return [
        ManifestEntry(
            path=entry.path,
            mode=entry.mode,
            sha256=hashlib.sha256(
                _read_blob(checkout_dir, oids[entry.path], env)
            ).hexdigest(),
        )
        if entry.mode != _MODE_GITLINK and protected_path_reason(entry.path)
        else entry
        for entry in entries
    ]


def _merged_base_digests(
    checkout_dir: Path, merged_base_sha: str | None, env: dict[str, str]
) -> Callable[[str], str | None] | None:
    """The merged base's content reader, or ``None`` when there is no answer.

    The checkout escape hatch's counterpart of canonical's reader (#2362): a
    remediation checkout merges its base, so a protected path whose bytes are
    the merged base's own was brought in by that merge, not authored here.
    *merged_base_sha* is the HOST's answer — kew's own integration or the
    remote base head — never a ref the checkout holds; it is only looked up in
    the checkout's object store, by id. Best effort, as canonically: a base the
    checkout does not hold, or that will not list, takes the exemption away
    and every protected path stops for a person as before.
    """
    if not merged_base_sha:
        return None
    try:
        listing = _git(
            checkout_dir,
            "ls-tree",
            "-r",
            "-z",
            "--full-tree",
            f"{merged_base_sha}^{{commit}}",
            env=env,
            step="push",
        )
    except PublicationError as exc:
        _warn(f"merged base {merged_base_sha[:12]} is unavailable: {exc}")
        return None
    blobs: dict[str, str] = {}
    for record in _split_z(listing):
        meta, _, path = record.partition("\t")
        _mode, kind, oid = meta.split(" ")
        if kind == "blob":
            blobs[path] = oid

    def digest(path: str) -> str | None:
        oid = blobs.get(path)
        if oid is None:
            return None
        try:
            return hashlib.sha256(_read_blob(checkout_dir, oid, env)).hexdigest()
        except PublicationError as exc:
            _warn(f"merged base blob for {path} could not be read: {exc}")
            return None

    return digest


def filter_checkout_range(
    checkout_dir: Path,
    *,
    base_sha: str,
    head_sha: str,
    env: dict[str, str],
    merged_base_sha: str | None = None,
) -> None:
    """Refuse or hold *base_sha*..*head_sha* as canonical publication would.

    *env* is the object environment the scan and the push read with, so the
    filter judges the same objects they do. A refusal raises
    :class:`~kew.publication.PublicationError`; a flag raises
    :class:`~kew.publication_canonical.PublicationHeld`, as canonically.

    *merged_base_sha* is the base commit this checkout merged, as the host
    knows it. A protected-path write byte-identical to that commit's own is
    not held, exactly as canonical publication exempts it (#2362).
    """
    entries, deletions, oids = _range_manifest(
        checkout_dir, base_sha=base_sha, head_sha=head_sha, env=env
    )
    if not entries and not deletions:
        return
    modes = _base_modes(checkout_dir, base_sha, env)

    def read_text(path: str) -> str:
        return _read_blob(checkout_dir, oids[path], env).decode(
            "utf-8", errors="strict"
        )

    verdict = filter_manifest(
        _with_digests(checkout_dir, entries, oids, env),
        deletions=deletions,
        existing_modes=modes,
        destination_paths=list(modes),
        read_text=read_text,
        merged_base_digest=_merged_base_digests(checkout_dir, merged_base_sha, env),
    )
    if verdict.refused:
        listed = "; ".join(verdict.refusal_reasons()[:5])
        raise PublicationError(
            "push", f"checkout publication refused by the publication filter: {listed}"
        )
    if verdict.needs_human:
        raise PublicationHeld(verdict.review_reasons())
