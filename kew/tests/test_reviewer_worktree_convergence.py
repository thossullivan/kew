"""Wave 2 convergence guard: the reviewer and implementation dispatch share
one operator-routed clone and fetch path in ``kew.worktree``. The remote-head
fetch (#1004) and local-clone upstream setup (#1002) cannot diverge again.

The load-bearing test here is ``test_shared_remote_head_fetch_flips_both_callers``:
a single mutation to the shared operator fetch step must break BOTH
``materialize_reviewer_worktree`` and ``dispatch_core._materialize_checkout``.
If either caller stops routing through the shared step, its arm goes green while
the other stays red — surfacing the divergence the A/C bug class was made of.
"""

import shutil
from pathlib import Path

import pytest

from kew import worktree
from kew.config import DispatchConfig
from kew.dispatch_core import _materialize_checkout
from kew.review_dispatch import materialize_reviewer_worktree
from kew.subprocess_utils import TOOL_PATHS, run


@pytest.fixture(autouse=True)
def _register_git(monkeypatch):
    git = shutil.which("git")
    assert git is not None, "git required for convergence tests"
    monkeypatch.setitem(TOOL_PATHS, "git", git)


def _git(cwd: Path, *args: str) -> None:
    run(["git", *args], cwd=cwd, check=True)


def _remote_only_head_repo(tmp_path: Path) -> tuple[Path, str, str]:
    """origin (bare) + a repo_root clone at base + an agent clone that pushes a
    PR head origin holds but repo_root has NOT fetched (the #1004 precondition).

    Returns ``(repo_root, base_sha, remote_only_head_sha)``.
    """
    origin = tmp_path / "origin.git"
    run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    repo = tmp_path / "repo_root"
    run(["git", "clone", "-q", str(origin), str(repo)], check=True)
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "t")
    (repo / "code.py").write_text("x = 1\n")
    (repo / "CLAUDE.md").write_text("trusted base context\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "push", "-q", "origin", "main")
    base_sha = run(["git", "rev-parse", "HEAD"], cwd=repo, check=True).stdout.strip()
    _git(repo, "checkout", "--detach", base_sha)
    _git(repo, "branch", "-D", "main")

    agent = tmp_path / "agent"
    run(["git", "clone", "-q", str(origin), str(agent)], check=True)
    _git(agent, "config", "user.email", "a@a.a")
    _git(agent, "config", "user.name", "a")
    (agent / "code.py").write_text("x = 2  # the PR change\n")
    _git(agent, "add", "-A")
    _git(agent, "commit", "-q", "-m", "pr work")
    head_sha = run(["git", "rev-parse", "HEAD"], cwd=agent, check=True).stdout.strip()
    _git(agent, "push", "-q", "origin", "HEAD:refs/heads/agent/issue-996")

    # Precondition: repo_root does not carry the PR head yet.
    missing = run(["git", "cat-file", "-e", head_sha], cwd=repo, check=False)
    assert missing.returncode != 0
    return repo, base_sha, head_sha


def test_detached_repo_root_remote_only_head_has_clean_pinned_diff_boundary(tmp_path):
    repo, base_sha, head_sha = _remote_only_head_repo(tmp_path)

    # The operator checkout is detached and has no local branch-name base ref.
    assert (
        run(
            ["git", "show-ref", "--verify", "refs/heads/main"], cwd=repo, check=False
        ).returncode
        != 0
    )

    wt = materialize_reviewer_worktree(repo, head_sha, base_ref="main")
    try:
        assert (
            run(
                ["git", "rev-parse", "refs/kew/review/base"],
                cwd=wt,
                check=True,
            ).stdout.strip()
            == base_sha
        )
        assert (
            run(
                ["git", "rev-parse", "refs/kew/review/head"],
                cwd=wt,
                check=True,
            ).stdout.strip()
            == head_sha
        )
        assert run(["git", "status", "--short"], cwd=wt, check=True).stdout == ""
        changed = run(
            [
                "git",
                "diff",
                "--name-only",
                "refs/kew/review/base...refs/kew/review/head",
                "--",
            ],
            cwd=wt,
            check=True,
        ).stdout.splitlines()
        assert changed == ["code.py"]
        assert (wt / "CLAUDE.md").read_text() == "trusted base context\n"
    finally:
        shutil.rmtree(wt, ignore_errors=True)


def test_shared_remote_head_fetch_flips_both_callers(tmp_path, monkeypatch):
    """One mutation to the operator fetch breaks BOTH materialize callers.

    Both arms invoke the SAME shared upstream step: the reviewer fetches its
    remote-only head while pinning clone inputs, and dispatch fetches its base
    during refresh. Each error proves that an upstream fetch failure surfaces.
    """
    boom = RuntimeError("shared remote-head fetch mutated")

    def _mutated(*_args, **_kwargs):
        raise boom

    monkeypatch.setattr(worktree, "_fetch_commit_from_origin", _mutated)

    # --- reviewer arm: remote-only head forces the shared fetch ---
    repo, base_sha, head_sha = _remote_only_head_repo(tmp_path)
    with pytest.raises(RuntimeError, match="shared remote-head fetch mutated"):
        materialize_reviewer_worktree(repo, head_sha, base_ref=base_sha)

    # --- dispatch arm: base refresh routes through the same shared fetch ---
    upstream = tmp_path / "d_upstream"
    upstream.mkdir()
    _git(upstream, "init", "-q", "-b", "main")
    _git(upstream, "config", "user.email", "t@t.t")
    _git(upstream, "config", "user.name", "t")
    (upstream / "base.txt").write_text("a\n")
    _git(upstream, "add", "-A")
    _git(upstream, "commit", "-q", "-m", "A")
    local = tmp_path / "d_local"
    run(["git", "clone", "-q", str(upstream), str(local)], check=True)
    monkeypatch.chdir(local)
    with pytest.raises(RuntimeError, match="shared remote-head fetch mutated"):
        _materialize_checkout("5", "agent/issue-5", DispatchConfig())


def test_reviewer_materialize_reuses_shared_clone_and_fetch(tmp_path):
    """The reviewer's clone/fetch/checkout is the shared worktree path — a
    remote-only head is fetched and detached-checked-out with zero reviewer-local
    clone/fetch code (only the head-ref mask stays reviewer-specific)."""
    repo, base_sha, head_sha = _remote_only_head_repo(tmp_path)
    wt = materialize_reviewer_worktree(repo, head_sha, base_ref=base_sha)
    try:
        assert "the PR change" in (wt / "code.py").read_text()
        # Detached at the recorded head (not on a branch).
        branch = run(
            ["git", "-C", str(wt), "rev-parse", "--abbrev-ref", "HEAD"], check=True
        ).stdout.strip()
        assert branch == "HEAD"
    finally:
        shutil.rmtree(wt, ignore_errors=True)
