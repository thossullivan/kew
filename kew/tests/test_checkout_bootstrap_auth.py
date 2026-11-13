"""Authenticated preparation for host and sandbox agent checkouts."""

import os
import shutil
from pathlib import Path

import pytest

from kew import subprocess_utils, worktree
from kew.config import DispatchConfig


@pytest.fixture(autouse=True)
def _git_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    git = shutil.which("git")
    assert git is not None
    monkeypatch.setitem(subprocess_utils.TOOL_PATHS, "git", git)


def _bootstrap_git(repo: Path, *args: str) -> str:
    """Run real Git through kew's subprocess boundary."""
    return subprocess_utils.run(["git", *args], cwd=repo).stdout.strip()


def _private_bootstrap_repo(tmp_path: Path) -> tuple[Path, str, str, str]:
    """Keep the operator stale while origin gains a base and remote-only head."""
    origin = tmp_path / "origin.git"
    _bootstrap_git(tmp_path, "init", "--bare", "-q", "-b", "main", str(origin))
    for key, value in (("user.name", "Test"), ("user.email", "test@example.test")):
        _bootstrap_git(origin, "config", key, value)
    tree = _bootstrap_git(origin, "mktree")
    first = _bootstrap_git(origin, "commit-tree", tree, "-m", "initial")
    _bootstrap_git(origin, "update-ref", "refs/heads/main", first)
    operator = tmp_path / "operator"
    _bootstrap_git(tmp_path, "clone", "-q", str(origin), str(operator))
    base = _bootstrap_git(origin, "commit-tree", tree, "-p", first, "-m", "new base")
    head = _bootstrap_git(origin, "commit-tree", tree, "-p", base, "-m", "PR head")
    _bootstrap_git(origin, "update-ref", "refs/heads/main", base)
    _bootstrap_git(origin, "update-ref", "refs/heads/feature", head)
    upstream = f"file://user:password@localhost{origin}"
    _bootstrap_git(operator, "remote", "set-url", "origin", upstream)
    return operator, base, head, upstream


@pytest.mark.parametrize(
    "preparation", ["host", "sandbox", "reviewer", "review_checkout"]
)
def test_bootstrap_fetch_uses_operator_authentication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, preparation: str
) -> None:
    from kew import dispatch_core, review_checkout, review_dispatch

    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    operator, base, head, upstream = _private_bootstrap_repo(tmp_path)
    monkeypatch.chdir(operator)
    real_run = subprocess_utils.run
    transfers = []
    upstream_fetches = []

    def authenticated_run(cmd, **kwargs):
        cwd = Path(kwargs.get("cwd") or Path.cwd())
        if "-C" in cmd:
            cwd = Path(cmd[cmd.index("-C") + 1])
        if "fetch" in cmd:
            args = cmd[cmd.index("fetch") + 1 :]
            if cwd == operator:
                assert "origin" in args
                assert (
                    _bootstrap_git(operator, "config", "remote.origin.url") == upstream
                )
                upstream_fetches.append(cmd)
            else:
                # This gate models private access without a network server.
                # An agent fetch must use the authenticated operator's objects.
                assert str(operator) in args, "agent fetch must use the operator path"
                assert all("password" not in arg and "@" not in arg for arg in args)
                transfers.append(cmd)
        if "ls-remote" in cmd:
            assert cwd == operator, "branch discovery must use operator authentication"
        return real_run(cmd, **kwargs)

    for module in (worktree, dispatch_core, review_dispatch, review_checkout):
        monkeypatch.setattr(module, "run", authenticated_run)
    checkout = tmp_path / "agent"
    config = DispatchConfig()
    config.branches.base = "main"
    if preparation == "host":
        worktree._clone_checkout_on(
            checkout, "agent/test", start_point="main", exact_sha=base
        )
        expected = base
    elif preparation == "sandbox":
        checkout = dispatch_core._materialize_checkout("1218", "feature", config)
        expected = head
    elif preparation == "reviewer":
        # This caller receives an explicit source independent of the current directory.
        monkeypatch.chdir(tmp_path)
        checkout = review_dispatch.materialize_reviewer_worktree(operator, head, base)
        expected = head
    else:
        review_checkout._materialize(
            checkout, branch_name="feature", head_sha=head, detached=False
        )
        expected = head

    assert transfers and upstream_fetches
    assert _bootstrap_git(checkout, "rev-parse", "HEAD") == expected
    fetch_head = checkout / ".git" / "FETCH_HEAD"
    if preparation == "reviewer":
        # The reviewer transfers both pins during clone without writing the
        # checkout's shared fetch pseudo-ref. HEAD still proves the exact pin.
        assert not fetch_head.exists()
    else:
        assert _bootstrap_git(checkout, "rev-parse", "FETCH_HEAD") == expected
    assert _bootstrap_git(checkout, "config", "remote.origin.url") == upstream.replace(
        "user:password@", ""
    )
    for name in ("config", "FETCH_HEAD", "packed-refs"):
        metadata = checkout / ".git" / name
        if metadata.exists():
            assert "user:password@" not in metadata.read_text()
    assert _bootstrap_git(operator, "config", "remote.origin.url") == upstream
    assert _bootstrap_git(operator, "for-each-ref", "refs/kew/bootstrap/") == ""
