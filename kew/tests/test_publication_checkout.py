"""The checkout escape hatch commits inertly and is filtered like canonical (#2634).

Every assertion is on an EFFECT — a marker file a planted program would have
written, a blob's bytes, a raised refusal — never on captured output.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from kew import subprocess_utils
from kew.publication import PublicationError
from kew.publication_canonical import PublicationHeld
from kew.publication_checkout import commit_checkout_worktree, filter_checkout_range
from kew.worktree import _commit_publication_worktree, checkout_object_env


@pytest.fixture(autouse=True)
def _git_allowed():
    saved = subprocess_utils.TOOL_PATHS.copy()
    subprocess_utils._register_optional_tool("git")
    yield
    subprocess_utils.TOOL_PATHS.clear()
    subprocess_utils.TOOL_PATHS.update(saved)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    repo = tmp_path / "checkout"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "user.email", "t@invalid.local")
    (repo / "kept.txt").write_text("kept\n")
    (repo / "gone.txt").write_text("gone\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    return repo


def _program(path: Path, marker: Path) -> Path:
    path.write_text(f"#!/bin/sh\ntouch {marker}\ncat\n")
    path.chmod(0o755)
    return path


def test_commits_what_add_all_would(
    checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for role in ("AUTHOR", "COMMITTER"):
        for part in ("NAME", "EMAIL"):
            monkeypatch.delenv(f"GIT_{role}_{part}", raising=False)
    base = _git(checkout, "rev-parse", "HEAD")
    (checkout / "gone.txt").unlink()
    (checkout / "new").mkdir()
    (checkout / "new" / "tool.sh").write_text("#!/bin/sh\n")
    (checkout / "new" / "tool.sh").chmod(0o755)
    (checkout / ".gitignore").write_text("ignored.log\n")
    (checkout / "ignored.log").write_text("noise\n")

    assert commit_checkout_worktree(checkout, "feat: work") is True

    assert _git(checkout, "rev-parse", "HEAD~1") == base
    listing = _git(checkout, "ls-tree", "-r", "HEAD")
    assert "gone.txt" not in listing
    assert "ignored.log" not in listing
    assert "100755 blob" in _git(checkout, "ls-tree", "HEAD", "new/tool.sh")
    assert _git(checkout, "log", "-1", "--format=%an <%ae>%n%s") == (
        "kew <kew@invalid.local>\nfeat: work"
    )
    assert _git(checkout, "status", "--porcelain") == ""
    assert commit_checkout_worktree(checkout, "again") is False


def test_no_program_the_checkout_names_runs(checkout: Path, tmp_path: Path) -> None:
    hooks = checkout / ".git" / "evil-hooks"
    hooks.mkdir()
    marks = tmp_path / "marks"
    marks.mkdir()
    for hook in ("pre-commit", "commit-msg", "post-commit", "post-index-change"):
        _program(hooks / hook, marks / hook)
    _git(checkout, "config", "core.hooksPath", str(hooks))
    fsmonitor = _program(tmp_path / "fsmonitor", marks / "fsmonitor")
    _git(checkout, "config", "core.fsmonitor", str(fsmonitor))
    clean = _program(tmp_path / "clean", marks / "clean")
    _git(checkout, "config", "filter.evil.clean", str(clean))
    (checkout / ".git" / "info" / "attributes").write_text("* filter=evil\n")
    gpg = _program(tmp_path / "gpg", marks / "gpg")
    _git(checkout, "config", "commit.gpgSign", "true")
    _git(checkout, "config", "gpg.program", str(gpg))
    (checkout / "kept.txt").write_text("changed\n")

    _commit_publication_worktree(checkout, "fix: change")

    assert sorted(os.listdir(marks)) == []
    assert _git(checkout, "show", "HEAD:kept.txt") == "changed"
    assert _git(checkout, "log", "-1", "--format=%s") == "fix: change"


def test_a_nested_repository_is_refused(checkout: Path) -> None:
    nested = checkout / "sub"
    nested.mkdir()
    _git(nested, "init", "-q")

    with pytest.raises(PublicationError, match="nested repository"):
        commit_checkout_worktree(checkout, "x")


def _filter(checkout: Path, base: str) -> None:
    filter_checkout_range(
        checkout,
        base_sha=base,
        head_sha=_git(checkout, "rev-parse", "HEAD"),
        env=checkout_object_env(),
    )


def test_filter_passes_ordinary_work(checkout: Path) -> None:
    base = _git(checkout, "rev-parse", "HEAD")
    (checkout / "kept.txt").write_text("edit\n")
    (checkout / "gone.txt").unlink()
    commit_checkout_worktree(checkout, "work")

    _filter(checkout, base)


def test_filter_refuses_an_agent_made_symlink(checkout: Path) -> None:
    base = _git(checkout, "rev-parse", "HEAD")
    (checkout / "link").symlink_to("/etc/passwd")
    # The agent's own commit, not kew's: the range is what is filtered.
    _git(checkout, "add", "-A")
    _git(checkout, "commit", "-q", "-m", "agent")

    with pytest.raises(PublicationError, match="publication filter"):
        _filter(checkout, base)


def test_filter_refuses_a_gitattributes_driver(checkout: Path) -> None:
    base = _git(checkout, "rev-parse", "HEAD")
    (checkout / ".gitattributes").write_text("* filter=evil\n")
    commit_checkout_worktree(checkout, "work")

    with pytest.raises(PublicationError, match="publication filter"):
        _filter(checkout, base)


def test_filter_holds_a_protected_path(checkout: Path) -> None:
    base = _git(checkout, "rev-parse", "HEAD")
    workflows = checkout / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text("on: push\n")
    commit_checkout_worktree(checkout, "work")

    with pytest.raises(PublicationHeld):
        _filter(checkout, base)


def _merged_base_workflow(checkout: Path, *, agent_edit: str | None) -> tuple[str, str]:
    """A branch that merged a base which moved a workflow; ``(branch_base, main)``."""
    workflows = checkout / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text("on: push\n")
    _git(checkout, "add", "-A")
    _git(checkout, "commit", "-q", "-m", "workflow")
    fork = _git(checkout, "rev-parse", "HEAD")
    _git(checkout, "checkout", "-q", "-b", "agent/x")
    (checkout / "kept.txt").write_text("agent\n")
    _git(checkout, "commit", "-q", "-am", "agent work")
    branch_base = _git(checkout, "rev-parse", "HEAD")
    _git(checkout, "checkout", "-q", "main")
    (workflows / "ci.yml").write_text("on: [push, pull_request]\n")
    _git(checkout, "commit", "-q", "-am", "main moves ci")
    main = _git(checkout, "rev-parse", "HEAD")
    _git(checkout, "checkout", "-q", "agent/x")
    _git(checkout, "merge", "-q", "--no-edit", main)
    if agent_edit is not None:
        (workflows / "ci.yml").write_text(agent_edit)
    commit_checkout_worktree(checkout, "remediation")
    assert fork != main
    return branch_base, main


def _filter_merged(checkout: Path, base: str, merged_base_sha: str | None) -> None:
    filter_checkout_range(
        checkout,
        base_sha=base,
        head_sha=_git(checkout, "rev-parse", "HEAD"),
        env=checkout_object_env(),
        merged_base_sha=merged_base_sha,
    )


def test_filter_does_not_hold_a_protected_path_the_merged_base_brought(
    checkout: Path,
) -> None:
    base, main = _merged_base_workflow(checkout, agent_edit=None)

    _filter_merged(checkout, base, main)


def test_filter_holds_a_protected_path_without_the_merged_base(
    checkout: Path,
) -> None:
    base, _main = _merged_base_workflow(checkout, agent_edit=None)

    with pytest.raises(PublicationHeld):
        _filter_merged(checkout, base, None)
    with pytest.raises(PublicationHeld):
        _filter_merged(checkout, base, "0" * 40)


def test_filter_holds_an_agent_edit_on_top_of_the_merged_base(
    checkout: Path,
) -> None:
    base, main = _merged_base_workflow(checkout, agent_edit="on: workflow_dispatch\n")

    with pytest.raises(PublicationHeld):
        _filter_merged(checkout, base, main)
