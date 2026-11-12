"""A merge that could not run is not a merge with conflicts (#1455).

`_apply_resolve_strategy` treated ANY non-zero `git merge` as conflicts, so an
operational failure — no committer identity, a held index.lock, a refusing hook,
a missing ref — came back as ``ConflictInfo(conflicted_files=[])``: "there are
conflicts, and there are no conflicted files". Downstream reads ConflictInfo as
"dispatch an agent to resolve these", so the result was an agent sent to resolve
nothing against a tree where the merge had not happened, while git's stderr —
which said exactly what was wrong — was discarded.

Real git throughout: the distinction is between two exit states of a real merge,
and a mock would have to encode the very assumption under test.
"""

import subprocess
from pathlib import Path

import pytest

from kew import subprocess_utils
from kew.worktree import BaseIntegrationError, ConflictInfo, integrate_base_branch


@pytest.fixture(autouse=True)
def _git_allowed():
    saved = subprocess_utils.TOOL_PATHS.copy()
    subprocess_utils._register_optional_tool("git")
    yield
    subprocess_utils.TOOL_PATHS.clear()
    subprocess_utils.TOOL_PATHS.update(saved)


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _break_committer_identity(repo: Path, tmp_path: Path, monkeypatch) -> None:
    """Leave git with no usable identity, deterministically.

    A clone carries no local identity, but git falls back to the machine's
    global config — so on a developer box the merge would succeed and the test
    would prove nothing. Point global/system config at an empty file and forbid
    the hostname guess, so the outcome does not depend on whose machine it is.
    """
    empty = tmp_path / "empty-gitconfig"
    empty.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(empty))
    _git("config", "user.useConfigOnly", "true", cwd=repo)


def _repo_with_diverged_base(tmp_path: Path, *, conflicting: bool) -> Path:
    """A checkout whose branch has diverged from origin/main.

    ``conflicting=True`` makes both sides edit the same line, so the merge has
    real conflicts. ``conflicting=False`` makes them edit different files, so
    the merge is clean *content-wise* and only fails if git itself cannot run.
    """
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", str(origin)], check=True)

    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", "-q", str(origin), str(seed)], check=True)
    _git("config", "user.email", "seed@example.com", cwd=seed)
    _git("config", "user.name", "Seed", cwd=seed)
    (seed / "shared.txt").write_text("base\n")
    _git("add", "-A", cwd=seed)
    _git("commit", "-qm", "base", cwd=seed)
    _git("branch", "-M", "main", cwd=seed)
    _git("push", "-q", "origin", "main", cwd=seed)

    # The work branch, pushed, then base moves on underneath it.
    _git("checkout", "-qb", "agent/issue-1", cwd=seed)
    (seed / "shared.txt").write_text("agent's version\n")
    _git("commit", "-qam", "agent work", cwd=seed)
    _git("push", "-q", "origin", "agent/issue-1", cwd=seed)

    _git("checkout", "-q", "main", cwd=seed)
    if conflicting:
        (seed / "shared.txt").write_text("base moved on\n")
    else:
        (seed / "unrelated.txt").write_text("base moved on\n")
        _git("add", "-A", cwd=seed)
    _git("commit", "-qam", "base advances", cwd=seed)
    _git("push", "-q", "origin", "main", cwd=seed)

    work = tmp_path / "work"
    subprocess.run(
        ["git", "clone", "-q", "-b", "agent/issue-1", str(origin), str(work)],
        check=True,
    )
    return work


def test_a_merge_that_cannot_run_raises_instead_of_reporting_conflicts(
    tmp_path: Path, monkeypatch
):
    """No committer identity: the merge never happens. That is not a conflict."""
    work = _repo_with_diverged_base(tmp_path, conflicting=False)
    # The exact shape the defect was found through (#1454's identity bug).
    _break_committer_identity(work, tmp_path, monkeypatch)

    with pytest.raises(BaseIntegrationError) as caught:
        integrate_base_branch(worktree_dir=work, base_branch="main", strategy="merge")

    assert caught.value.base_branch == "main"
    # git's own diagnosis is carried, not discarded — the first instance of this
    # cost a CI round-trip because the symptom named the wrong subsystem.
    assert "ident" in str(caught.value).lower() or "email" in str(caught.value).lower()


def test_a_real_conflict_is_still_reported_as_a_conflict(tmp_path: Path):
    """The raise must not swallow the case the function exists to detect."""
    work = _repo_with_diverged_base(tmp_path, conflicting=True)
    _git("config", "user.email", "worker@example.com", cwd=work)
    _git("config", "user.name", "Worker", cwd=work)

    conflict = integrate_base_branch(
        worktree_dir=work, base_branch="main", strategy="merge"
    )

    assert isinstance(conflict, ConflictInfo)
    assert conflict.conflicted_files == ["shared.txt"]
    assert conflict.base_branch == "main"


def test_a_clean_integration_still_returns_none(tmp_path: Path):
    work = _repo_with_diverged_base(tmp_path, conflicting=False)
    _git("config", "user.email", "worker@example.com", cwd=work)
    _git("config", "user.name", "Worker", cwd=work)

    assert (
        integrate_base_branch(worktree_dir=work, base_branch="main", strategy="merge")
        is None
    )


def test_no_conflict_info_can_ever_carry_an_empty_file_list(
    tmp_path: Path, monkeypatch
):
    """The invariant, stated directly.

    git cannot report conflicts with no unmerged paths, so a ConflictInfo with
    an empty ``conflicted_files`` is never a truthful value from this seam. Both
    reachable non-clean outcomes are checked: the conflicting one yields files,
    the un-runnable one raises.
    """
    conflicting = _repo_with_diverged_base(tmp_path / "a", conflicting=True)
    _git("config", "user.email", "w@example.com", cwd=conflicting)
    _git("config", "user.name", "W", cwd=conflicting)
    result = integrate_base_branch(
        worktree_dir=conflicting, base_branch="main", strategy="merge"
    )
    assert result is not None and result.conflicted_files

    broken = _repo_with_diverged_base(tmp_path / "b", conflicting=False)
    _break_committer_identity(broken, tmp_path, monkeypatch)
    with pytest.raises(BaseIntegrationError):
        integrate_base_branch(worktree_dir=broken, base_branch="main", strategy="merge")
