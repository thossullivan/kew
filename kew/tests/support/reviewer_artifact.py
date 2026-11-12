"""Put a deployed reviewer artifact on a Git ref, not in the working tree.

The review launch and the prompt preview read the deployed copy out of the
base ref (#3087), so a test that means "the base ref deploys this copy" has to
commit it there. This writes the blob through plumbing — a private index, then
``commit-tree`` and ``update-ref`` — so neither the working tree, the real
index nor ``HEAD`` moves.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from kew.review_contract import reviewer_prompt_artifact


def _git(
    repo: Path, *args: str, env: dict[str, str] | None = None, data: bytes | None = None
) -> str:
    done = subprocess.run(  # noqa: S603 — test fixture over a tmp repo
        ["git", "-C", str(repo), *args],  # noqa: S607
        input=data,
        capture_output=True,
        check=True,
        env=env,
    )
    return done.stdout.decode().strip()


def git_repo(root: Path) -> Path:
    """*root* as a repository on ``main`` with one empty commit, made if absent."""
    if not (root / ".git").exists():
        root.mkdir(parents=True, exist_ok=True)
        _git(root, "init", "-q", "-b", "main")
        _git(root, "config", "user.email", "t@t.t")
        _git(root, "config", "user.name", "t")
        _git(root, "commit", "-q", "--allow-empty", "-m", "root")
    return root


def commit_reviewer_artifact(
    repo: Path,
    text: str,
    *,
    ref: str = "main",
    under: str = "",
    mode: str = "100644",
) -> str:
    """Commit *text* as the deployed reviewer artifact on *ref*; return the commit.

    *under* puts it below that subdirectory of the repository, where a project
    rooted there deploys it. *mode* is its tree entry's mode: ``120000``
    commits *text* as a symlink's target path.
    """
    target = reviewer_prompt_artifact().target
    path = f"{under.strip('/')}/{target}" if under else target
    blob = _git(repo, "hash-object", "-w", "--stdin", data=text.encode("utf-8"))
    parent = _git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{ref}")
    env = {**os.environ, "GIT_INDEX_FILE": str(repo / ".git" / "kew-test-index")}
    _git(repo, "read-tree", parent, env=env)
    _git(repo, "update-index", "--add", "--cacheinfo", f"{mode},{blob},{path}", env=env)
    tree = _git(repo, "write-tree", env=env)
    commit = _git(
        repo, "commit-tree", tree, "-p", parent, "-m", "deploy reviewer", env=env
    )
    _git(repo, "update-ref", f"refs/heads/{ref}", commit)
    return commit


def lose_blob(repo: Path, text: str) -> None:
    """Delete *text*'s loose object from *repo*, as a broken clone loses one.

    The trees that list it still do, so the base ref names a deployed copy
    that git can no longer read.
    """
    lose_object(repo, _git(repo, "hash-object", "--stdin", data=text.encode("utf-8")))


def lose_object(repo: Path, spec: str) -> None:
    """Delete the loose object *spec* names in *repo* (``main:.claude/kew`` is
    the tree that lists the deployed copy), so Git can no longer read it."""
    oid = _git(repo, "rev-parse", spec)
    (repo / ".git" / "objects" / oid[:2] / oid[2:]).unlink()
