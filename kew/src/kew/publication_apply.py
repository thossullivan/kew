"""W1.1 node 5a: turn a filtered artifact into a git tree, without a worktree.

The broker's last step is "write the agent's work somewhere the agent never
touched". The obvious way to do that is to check out the base, copy the files
in, and commit. This module deliberately does not do that.

**Nothing here writes a working tree.** Content goes straight to the object
database via ``hash-object``, and the paths go into a throwaway *index* via
``update-index --index-info``. A path in an index is a string in a data
structure; a path on a filesystem is a traversal, a symlink to follow, a case
collision, a directory that used to be a file. The filter (D4) rejects all of
those, but a guarantee that has to survive a filesystem round-trip is a
guarantee you have to re-prove at the far end. Here there is no far end: the
worst an approved path can do to an index is occupy a slot in it.

That also makes the result exact rather than approximate. Applying the diff
base→HEAD onto base must reproduce HEAD's tree *bit for bit*, so comparing the
tree this builds against the tree the legacy path publishes is a total test of
whether the broker drops anything — one sha, no allowances. That comparison is
node 5's whole purpose, and it only means something because this step cannot
quietly normalize a mode, drop an empty directory, or reorder anything.

The repository passed in must be one **no agent can write** and must already
contain the base commit. Reading a repository runs code from it, so pointing
this at the agent's checkout would reintroduce exactly the execution the broker
exists to remove.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

from kew.subprocess_utils import run_bytes

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from kew.publication_filter import ManifestEntry

__all__ = [
    "INERT_GIT_CONFIG",
    "ApplyFailed",
    "apply_to_tree",
    "inert_git_env",
    "trusted_git_env",
]

#: Hex width of an object id, by the repository's storage format. A removal
#: record's placeholder id is ignored by git but must still PARSE, so the width
#: has to be the destination's and not ours — see :func:`_null_oid`.
_OID_WIDTH = {"sha1": 40, "sha256": 64}


class ApplyFailed(Exception):
    """The tree could not be built. Never raised because of *content* — the
    artifact reader and the filter have both already passed — only because git
    itself refused, which means the environment is wrong, not the input."""


@dataclass(frozen=True)
class _Record:
    mode: str
    oid: str
    path: str

    def encode(self) -> bytes:
        # NUL-terminated, so a path may contain anything except NUL — including
        # the newline that would otherwise end the record early. The filter
        # rejects control characters, but this format is the reason it does not
        # have to be the only thing standing between a name and a mis-parse.
        return f"{self.mode} {self.oid}\t{self.path}\0".encode()


def trusted_git_env(**overrides: str) -> dict[str, str]:
    """The environment every host-side git call runs under.

    **Every inherited ``GIT_*`` is dropped.** ``GIT_DIR`` beats ``-C``: with it
    set, ``git -C <named> rev-parse --git-dir`` answers the *other* repository,
    so a caller would build trees in, and write objects into, one it never
    named. ``GIT_OBJECT_DIRECTORY``, ``GIT_ALTERNATE_OBJECT_DIRECTORIES`` and
    ``GIT_CONFIG_*`` redirect narrower pieces of the same thing — and
    ``GIT_CONFIG_*`` is how kew itself carries the publication token, so
    inheriting it is how that token reaches a process that has no business with
    it. An allowlist of *known-dangerous* names would need extending every time
    git adds one; dropping the prefix does not.

    Everything else is inherited, because git needs PATH and HOME to run at all
    and a bare environment would change behaviour rather than isolate it.

    This lives here, and is shared with :mod:`kew.publication_repository`,
    because one scrubbing rule with two implementations is one implementation
    that will eventually miss a variable the other caught.
    """
    env = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    # A replace ref would substitute a different object for the base tree, so
    # the tree built here would silently describe something other than the base
    # the manifest names.
    env["GIT_NO_REPLACE_OBJECTS"] = "1"
    # If the destination is ever a partial clone, a missing object should fail
    # here rather than quietly reach the network mid-publication.
    env["GIT_NO_LAZY_FETCH"] = "1"
    env.update(overrides)
    return env


#: Configuration that names a program or an attribute source, switched off at
#: command scope so no repository-local, worktree or included file can turn it
#: back on: no hook (``post-index-change`` runs on any index write), no
#: fsmonitor, and no attributes file beyond the tree's own (#2569 review).
INERT_GIT_CONFIG = (
    ("core.hooksPath", os.devnull),
    # Empty, which git reads as off, rather than ``false`` (#2591).
    ("core.fsmonitor", ""),
    ("core.attributesFile", os.devnull),
)


def inert_git_env(**overrides: str) -> dict[str, str]:
    """:func:`trusted_git_env` with no file outside the repository read, and
    :data:`INERT_GIT_CONFIG` applied.

    The config travels as ``GIT_CONFIG_COUNT`` rather than ``-c`` so it also
    reaches git calls whose argv this caller does not build (the output scan):
    it is command scope either way, which beats every config file. Every
    inherited ``GIT_*`` is still dropped first, so these are the only
    command-scope entries there are.
    """
    env = trusted_git_env(
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_ATTR_NOSYSTEM="1",
        GIT_CONFIG_COUNT=str(len(INERT_GIT_CONFIG)),
    )
    for index, (key, value) in enumerate(INERT_GIT_CONFIG):
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    env.update(overrides)
    return env


def _git_env(index_file: Path) -> dict[str, str]:
    """The inert environment, pointed at this call's throwaway index.

    Inert rather than merely trusted: ``read-tree`` and ``update-index`` write
    an index, which is what fires a repository's ``post-index-change`` hook.
    """
    return inert_git_env(GIT_INDEX_FILE=str(index_file))


def _null_oid(repo: Path, env: dict[str, str]) -> str:
    """The all-zero object id at *repo*'s own hex width."""
    fmt = (
        _git(
            repo,
            ["rev-parse", "--show-object-format=storage"],
            env=env,
            what="rev-parse --show-object-format",
        )
        .decode("ascii", errors="strict")
        .strip()
    )
    width = _OID_WIDTH.get(fmt)
    if width is None:
        raise ApplyFailed(f"unknown object format {fmt!r}")
    return "0" * width


def _inert_args() -> list[str]:
    """:data:`INERT_GIT_CONFIG` as ``-c`` options."""
    return [arg for key, value in INERT_GIT_CONFIG for arg in ("-c", f"{key}={value}")]


def _git(
    repo: Path,
    args: Sequence[str],
    *,
    env: dict[str, str],
    stdin: bytes | None = None,
    what: str,
) -> bytes:
    # No hook, fsmonitor or attributes file: the index is a throwaway one this
    # module just made, and each of those names a program or input the
    # repository's own config chooses (#2569 review). Also in the env, which
    # a caller-built one may lack.
    result = run_bytes(
        ["git", "-C", str(repo), *_inert_args(), *args],
        stdin=stdin,
        env=env,
    )
    if result.returncode != 0:
        detail = (result.stderr or b"").decode("utf-8", errors="replace").strip()
        raise ApplyFailed(f"{what} failed: {detail or f'exit {result.returncode}'}")
    return result.stdout or b""


def apply_to_tree(
    repo: Path,
    *,
    base_sha: str,
    approved: Sequence[ManifestEntry],
    deletions: Sequence[str],
    content: Callable[[ManifestEntry], bytes],
    index_file: Path,
) -> str:
    """Build the tree that *base_sha* becomes once *approved* and *deletions*
    are applied, and return its sha.

    *repo* must contain *base_sha* and must not be writable by any agent.
    *index_file* must not exist; git creates it. It is the caller's to remove —
    taking a path rather than making one keeps this function free of cleanup
    that a caller with a ``tmp_path`` already does better.

    *content* is called once per approved entry and must return the bytes the
    artifact verified. It is a callback rather than an attribute of the entry so
    that the bytes are fetched by DIGEST at the point of use, and never carried
    around next to the attacker-chosen path they belong to.
    """
    # Every git call shares this index. The agent's index, worktree and stash
    # are not involved at any point — this repo has no relationship to them.
    env = _git_env(index_file)

    _git(repo, ["read-tree", f"{base_sha}^{{tree}}"], env=env, what="read-tree")

    records: list[_Record] = []
    for entry in approved:
        oid = (
            _git(
                repo,
                ["hash-object", "-w", "-t", "blob", "--stdin"],
                env=env,
                stdin=content(entry),
                what=f"hash-object for {entry.path!r}",
            )
            .decode("ascii", errors="strict")
            .strip()
        )
        if not oid:
            raise ApplyFailed(f"hash-object returned no id for {entry.path!r}")
        records.append(_Record(mode=entry.mode, oid=oid, path=entry.path))

    # Asked of the repository, and only when there is something to delete. A
    # removal's id is ignored by git but still has to parse, so a 40-zero
    # placeholder in a SHA-256 repository is "fatal: malformed index info" —
    # and since every record travels in one call, one deletion would fail the
    # whole apply. Hard-coding the width makes the module quietly SHA-1-only.
    null_oid = _null_oid(repo, env) if deletions else ""

    # Deletions go in the SAME update-index call as the writes. Two calls would
    # be two chances for one half to be forgotten, which is the exact defect the
    # filter's combined write/delete screening exists to prevent upstream; there
    # is no reason to reintroduce it one layer down.
    records.extend(_Record(mode="0", oid=null_oid, path=path) for path in deletions)

    if records:
        _git(
            repo,
            ["update-index", "-z", "--index-info"],
            env=env,
            stdin=b"".join(record.encode() for record in records),
            what="update-index",
        )

    tree = (
        _git(repo, ["write-tree"], env=env, what="write-tree")
        .decode("ascii", errors="strict")
        .strip()
    )
    if not tree:
        raise ApplyFailed("write-tree returned no tree id")
    return tree
