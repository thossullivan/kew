"""Fixtures for checkout object-isolation tests."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class NarrowCheckoutSource:
    """A source repository with objects a checkout must not receive."""

    repo: Path
    main_sha: str
    review_branch: str
    review_sha: str
    secret_blob: str
    unreachable_blob: str


def _git(
    repo: Path, *args: str, check: bool = True
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=check,
        capture_output=True,
        text=True,
    )


def _git_out(repo: Path, *args: str) -> str:
    return _git(repo, *args).stdout.strip()


def make_narrow_checkout_source(
    tmp_path: Path, *, packed: bool = False
) -> NarrowCheckoutSource:
    """Create public refs plus local-only and unreachable objects."""
    origin = tmp_path / "narrow-origin.git"
    _git(tmp_path, "init", "--bare", "-q", "-b", "main", str(origin))

    repo = tmp_path / "narrow-source"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.test")
    (repo / "main.txt").write_text("public main\n")
    _git(repo, "add", "main.txt")
    _git(repo, "commit", "-q", "-m", "main")
    main_sha = _git_out(repo, "rev-parse", "HEAD")
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-q", "-u", "origin", "main")

    review_branch = "agent/security-review"
    _git(repo, "switch", "-q", "-c", review_branch)
    (repo / "review.txt").write_text("review head\n")
    _git(repo, "add", "review.txt")
    _git(repo, "commit", "-q", "-m", "review head")
    review_sha = _git_out(repo, "rev-parse", "HEAD")
    _git(repo, "push", "-q", "origin", review_branch)
    _git(repo, "switch", "-q", "main")
    _git(repo, "branch", "-D", review_branch)

    (repo / "dropped.txt").write_text("unreachable object marker\n")
    _git(repo, "add", "dropped.txt")
    _git(repo, "commit", "-q", "-m", "dropped")
    dropped_sha = _git_out(repo, "rev-parse", "HEAD")
    unreachable_blob = _git_out(repo, "rev-parse", "HEAD:dropped.txt")
    _git(repo, "reset", "--hard", "HEAD~1")

    _git(repo, "switch", "-q", "--orphan", "secret-branch")
    _git(repo, "rm", "-q", "-rf", "--ignore-unmatch", ".")
    (repo / "secret.txt").write_text("secret branch object marker\n")
    _git(repo, "add", "secret.txt")
    _git(repo, "commit", "-q", "-m", "local secret")
    secret_blob = _git_out(repo, "rev-parse", "HEAD:secret.txt")
    _git(repo, "switch", "-q", "main")
    _git(repo, "reflog", "expire", "--expire=now", "--all")

    if packed:
        # Keep the otherwise unreachable commit referenced only while repacking.
        # Deleting the ref afterwards leaves its objects unreachable inside the
        # same bitmap pack as the unrelated branch.
        packed_ref = "refs/kew/tests/unreachable"
        _git(repo, "update-ref", packed_ref, dropped_sha)
        _git(repo, "repack", "-a", "-d", "-b")
        _git(repo, "update-ref", "-d", packed_ref)
        _git(repo, "reflog", "expire", "--expire=now", "--all")

        packed_oids: set[str] = set()
        for index in (repo / ".git" / "objects" / "pack").glob("*.idx"):
            for line in _git_out(repo, "verify-pack", "-v", str(index)).splitlines():
                oid = line.partition(" ")[0]
                if len(oid) in (40, 64):
                    packed_oids.add(oid)
        assert {secret_blob, unreachable_blob} <= packed_oids
        assert list((repo / ".git" / "objects" / "pack").glob("*.bitmap"))

    for oid in (secret_blob, unreachable_blob):
        assert _git(repo, "cat-file", "-e", oid, check=False).returncode == 0

    return NarrowCheckoutSource(
        repo=repo,
        main_sha=main_sha,
        review_branch=review_branch,
        review_sha=review_sha,
        secret_blob=secret_blob,
        unreachable_blob=unreachable_blob,
    )


def assert_narrow_checkout(
    source: NarrowCheckoutSource,
    checkout: Path,
    *,
    required_refs: tuple[str, ...],
    expected_refs: set[str],
) -> None:
    """Assert that the checkout contains exactly the requested history and refs."""

    actual_refs = set(
        _git_out(checkout, "for-each-ref", "--format=%(refname)").splitlines()
    )
    assert actual_refs == expected_refs, (
        f"checkout refs differ: actual={actual_refs}, expected={expected_refs}"
    )

    required_commits = [
        _git_out(source.repo, "rev-parse", "--verify", f"{ref}^{{commit}}")
        for ref in required_refs
    ]
    expected_objects = {
        line.partition(" ")[0]
        for line in _git_out(
            source.repo, "rev-list", "--objects", *required_commits
        ).splitlines()
    }
    actual_objects = set(
        _git_out(
            checkout,
            "cat-file",
            "--batch-all-objects",
            "--batch-check=%(objectname)",
        ).splitlines()
    )
    assert actual_objects == expected_objects, (
        f"checkout objects differ: actual={actual_objects}, expected={expected_objects}"
    )

    for label, oid in (
        ("secret branch", source.secret_blob),
        ("unreachable", source.unreachable_blob),
    ):
        present = _git(checkout, "cat-file", "-e", oid, check=False)
        assert present.returncode != 0, f"{label} blob reached the checkout"

    source_inodes = {
        (path.stat().st_dev, path.stat().st_ino)
        for path in (source.repo / ".git" / "objects").rglob("*")
        if path.is_file()
    }
    shared = [
        path
        for path in (checkout / ".git" / "objects").rglob("*")
        if path.is_file() and (path.stat().st_dev, path.stat().st_ino) in source_inodes
    ]
    assert not shared, f"checkout shares source object inodes: {shared}"
