"""A git repo whose PR head touches a merge gate path, or not (#3422).

``main`` holds one base commit and stays checked out; the PR head is one commit
on top of it on branch ``pr``. Review tests pass ``base="main"`` and the head
sha, so the reviewer's ``base...head`` diff is exactly the files named here.
"""

from __future__ import annotations

from pathlib import Path

from kew.subprocess_utils import run


def _git(repo: Path, *args: str) -> str:
    return run(["git", *args], cwd=repo, check=True).stdout.strip()


def _write(repo: Path, path: str, text: str) -> None:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)


def make_pr_repo(
    tmp_path: Path, *changed: str, moved: tuple[tuple[str, str], ...] = ()
) -> tuple[Path, str]:
    """``(repo, pr_head)``: the head changes each path in *changed*, and
    ``git mv``s each ``(source, target)`` in *moved* (the base holds every
    source, unchanged, so git reads the move as a rename)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "t")
    (repo / "code.py").write_text("x = 0\n")
    for source, _ in moved:
        _write(repo, source, f"#!/bin/sh\n# moved by the PR: {source}\nexit 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "checkout", "-q", "-b", "pr")
    for path in changed:
        _write(repo, path, f"# changed by the PR: {path}\n")
    for source, target in moved:
        (repo / target).parent.mkdir(parents=True, exist_ok=True)
        _git(repo, "mv", source, target)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "pr")
    head = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "main")
    return repo, head


def add_pr_commit(repo: Path, path: str = "code.py") -> str:
    """One more commit on branch ``pr`` (a remediation's push); its sha.
    ``main`` stays checked out."""
    _git(repo, "checkout", "-q", "pr")
    _write(repo, path, f"# remediated: {path}\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "remediation")
    head = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "main")
    return head


IGNORED_SUBMODULE = "scripts/vendor"
"""The gitlink :func:`add_ignored_submodule` writes: under a gate directory."""


def add_ignored_submodule(repo: Path, pointer: str) -> None:
    """Commit a gitlink at :data:`IGNORED_SUBMODULE` pointing at *pointer*,
    on the checked-out branch, whose ``.gitmodules`` entry sets
    ``ignore = all`` — which drops a change of its pointer from a plain
    ``git diff``. Nothing is cloned: the gitlink is written to the index."""
    _write(
        repo,
        ".gitmodules",
        f'[submodule "vendor"]\n\tpath = {IGNORED_SUBMODULE}\n'
        "\turl = ./vendor.git\n\tignore = all\n",
    )
    _git(repo, "add", ".gitmodules")
    _git(
        repo,
        "update-index",
        "--add",
        "--cacheinfo",
        f"160000,{pointer},{IGNORED_SUBMODULE}",
    )
    _git(repo, "commit", "-q", "-m", f"vendor at {pointer[:7]}")


def make_submodule_pr_repo(tmp_path: Path) -> tuple[Path, str]:
    """``(repo, pr_head)``: the base pins :data:`IGNORED_SUBMODULE` at one
    pointer, ``ignore = all``, and the head moves only the pointer."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "t")
    add_ignored_submodule(repo, "1" * 40)
    _git(repo, "checkout", "-q", "-b", "pr")
    add_ignored_submodule(repo, "2" * 40)
    head = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "main")
    return repo, head
