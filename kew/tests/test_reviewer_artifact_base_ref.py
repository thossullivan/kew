"""The deployed reviewer artifact is read from the base ref, never the checkout.

The operator's working tree, the reviewed head and the base ref each hold a
different copy of ``.claude/kew/adversarial-reviewer.md``. The review launch
and the prompt preview both run under the base ref's bytes, and the attested
``prompt_artifact_sha`` is the digest of those bytes (#3087).
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest
from support.reviewer_artifact import commit_reviewer_artifact, git_repo, lose_object

from kew.config import BranchConfig, DispatchConfig, LoggingConfig
from kew.prompt_previews import PREVIEW_RENDERERS
from kew.review_contract import (
    CollectedSidecar,
    ParsedVerdict,
    ReviewerPromptReadError,
    resolve_reviewer_prompt,
)
from kew.review_dispatch import (
    _REVIEW_BASE_REF,
    ReviewerLaunchContext,
    dispatch_reviewer,
)
from kew.run_store import SqliteRunStore
from kew.service.prompts import PromptSourceUnreadable, list_roles, role_preview
from kew.subprocess_utils import run

BASE = "# Reviewer rubric on main\n\nThe merged instructions.\n"
HEAD = "# Reviewer rubric on the feature branch\n\nUnmerged instructions.\n"
WORKTREE = "# Reviewer rubric edited in place\n\nUncommitted instructions.\n"


@pytest.fixture(autouse=True)
def _register_git(monkeypatch: pytest.MonkeyPatch) -> None:
    from kew.subprocess_utils import TOOL_PATHS

    git = shutil.which("git")
    assert git is not None, "git required for the base-ref read"
    monkeypatch.setitem(TOOL_PATHS, "git", git)


def _git(repo: Path, *args: str) -> str:
    return run(["git", *args], cwd=repo, check=True).stdout.strip()


def _three_copies(tmp_path: Path) -> tuple[Path, str, str]:
    """A repo on a feature branch: main, the head and the working tree each
    carry their own artifact. Returns ``(repo, base_commit, head_sha)``."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "t")
    (repo / "code.py").write_text("x = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "checkout", "-q", "-b", "feature")
    base_commit = commit_reviewer_artifact(repo, BASE, ref="main")
    deployed = repo / ".claude" / "kew" / "adversarial-reviewer.md"
    deployed.parent.mkdir(parents=True)
    deployed.write_text(HEAD)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "head")
    head = _git(repo, "rev-parse", "HEAD")
    deployed.write_text(WORKTREE)
    return repo, base_commit, head


def test_the_review_launch_and_the_preview_run_under_the_base_ref_copy(
    tmp_path: Path,
) -> None:
    repo, base_commit, head = _three_copies(tmp_path)
    base_sha = hashlib.sha256(BASE.encode()).hexdigest()
    captured: dict[str, ReviewerLaunchContext] = {}

    def launcher(ctx: ReviewerLaunchContext) -> CollectedSidecar:
        captured["ctx"] = ctx
        return CollectedSidecar(ctx.log_dir / "s.json", "d", ParsedVerdict("READY"))

    store = SqliteRunStore(str(tmp_path / "dispatch.db"))
    try:
        result = dispatch_reviewer(
            store,
            DispatchConfig(logging=LoggingConfig(dir=str(tmp_path / "logs"))),
            item_id="42",
            issue_num=42,
            head_sha=head,
            repo_root=repo,
            base_ref="main",
            max_review_usd=5.0,
            run_id="rev-base-ref",
            node_epic_ref="epic:demo",
            launcher=launcher,
        )
    finally:
        store.close()
    if result.worktree is not None:
        shutil.rmtree(result.worktree, ignore_errors=True)

    prompt = captured["ctx"].prompt
    assert prompt.startswith(BASE)
    assert "Unmerged instructions." not in prompt
    assert "Uncommitted instructions." not in prompt
    assert captured["ctx"].prompt_artifact_sha == base_sha
    assert result.prompt_artifact_sha == base_sha

    dto = role_preview("adversarial_review", PREVIEW_RENDERERS, None, repo)
    assert dto.preview.startswith(BASE)
    assert "Unmerged instructions." not in dto.preview
    assert "Uncommitted instructions." not in dto.preview
    assert dto.source.kind == "deployed-artifact"
    assert dto.source.sha256 == base_sha
    assert dto.source.base_ref == "main"
    assert dto.source.base_commit == base_commit


def test_a_base_ref_without_a_deployed_copy_ships_the_packaged_default(
    tmp_path: Path,
) -> None:
    """A copy only the working tree and the head carry governs nothing."""
    repo, _base_commit, _head = _three_copies(tmp_path)
    _git(repo, "update-ref", "refs/heads/main", "main~1")  # main before the deploy

    dto = role_preview("adversarial_review", PREVIEW_RENDERERS, None, repo)
    assert dto.source.kind == "pinned-artifact"
    assert dto.source.base_ref == "main"
    assert "Uncommitted instructions." not in dto.preview
    assert "Unmerged instructions." not in dto.preview


UNPUSHED = "# Reviewer rubric on an unpushed local main\n\nNever pushed.\n"


def _operator_with_origin(tmp_path: Path) -> tuple[Path, str, str]:
    """An operator clone whose origin's ``main`` deploys BASE and whose local
    ``main`` carries one more, unpushed, commit deploying UNPUSHED; the clone
    sits on a pushed feature branch. Returns ``(operator, origin_main, head)``."""
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git(upstream, "init", "-q", "-b", "main")
    _git(upstream, "config", "user.email", "t@t.t")
    _git(upstream, "config", "user.name", "t")
    (upstream / "code.py").write_text("x = 1\n")
    _git(upstream, "add", "-A")
    _git(upstream, "commit", "-q", "-m", "base")
    origin_main = commit_reviewer_artifact(upstream, BASE, ref="main")
    _git(upstream, "checkout", "-q", "-b", "feature", origin_main)
    (upstream / "code.py").write_text("x = 2\n")
    _git(upstream, "commit", "-q", "-am", "head")
    head = _git(upstream, "rev-parse", "HEAD")
    _git(upstream, "checkout", "-q", "main")

    operator = tmp_path / "operator"
    _git(tmp_path, "clone", "-q", str(upstream), str(operator))
    _git(operator, "config", "user.email", "t@t.t")
    _git(operator, "config", "user.name", "t")
    _git(operator, "fetch", "-q", "origin", "feature")
    commit_reviewer_artifact(operator, UNPUSHED, ref="main")
    return operator, origin_main, head


def _dispatch(tmp_path: Path, repo: Path, head: str) -> tuple[str, Path | None]:
    captured: dict[str, ReviewerLaunchContext] = {}

    def launcher(ctx: ReviewerLaunchContext) -> CollectedSidecar:
        captured["ctx"] = ctx
        return CollectedSidecar(ctx.log_dir / "s.json", "d", ParsedVerdict("READY"))

    store = SqliteRunStore(str(tmp_path / "dispatch.db"))
    try:
        result = dispatch_reviewer(
            store,
            DispatchConfig(logging=LoggingConfig(dir=str(tmp_path / "logs"))),
            item_id="42",
            issue_num=42,
            head_sha=head,
            repo_root=repo,
            base_ref="main",
            max_review_usd=5.0,
            run_id="rev-origin-base",
            node_epic_ref="epic:demo",
            launcher=launcher,
        )
    finally:
        store.close()
    return captured["ctx"].prompt, result.worktree


def test_the_launch_reads_the_prompt_off_the_base_commit_it_reviews_against(
    tmp_path: Path,
) -> None:
    """An unpushed commit on the operator's local ``main`` governs neither the
    reviewed base nor the reviewer: both are origin's ``main``, one commit."""
    operator, origin_main, head = _operator_with_origin(tmp_path)

    prompt, checkout = _dispatch(tmp_path, operator, head)
    try:
        assert checkout is not None
        pinned_base = _git(checkout, "rev-parse", _REVIEW_BASE_REF)
    finally:
        if checkout is not None:
            shutil.rmtree(checkout, ignore_errors=True)

    assert pinned_base == origin_main
    assert prompt.startswith(BASE)
    assert "Never pushed." not in prompt


def test_the_launch_reads_origins_base_when_there_is_no_local_branch(
    tmp_path: Path,
) -> None:
    operator, _origin_main, head = _operator_with_origin(tmp_path)
    _git(operator, "checkout", "-q", "--detach")
    _git(operator, "branch", "-q", "-D", "main")

    prompt, checkout = _dispatch(tmp_path, operator, head)
    if checkout is not None:
        shutil.rmtree(checkout, ignore_errors=True)

    assert prompt.startswith(BASE)


def test_the_preview_reads_origins_base_not_an_unpushed_local_commit(
    tmp_path: Path,
) -> None:
    operator, origin_main, _head = _operator_with_origin(tmp_path)

    dto = role_preview("adversarial_review", PREVIEW_RENDERERS, None, operator)

    assert dto.preview.startswith(BASE)
    assert "Never pushed." not in dto.preview
    assert dto.source.base_commit == origin_main


def test_the_preview_reads_the_configured_base_branch(tmp_path: Path) -> None:
    """A project whose base is not ``main`` previews the ref its review reads."""
    repo, _base_commit, _head = _three_copies(tmp_path)
    _git(repo, "branch", "-q", "trunk", "main")
    _git(repo, "update-ref", "refs/heads/main", "main~1")  # main deploys nothing
    config = DispatchConfig(branches=BranchConfig(base="trunk"))

    dto = role_preview("adversarial_review", PREVIEW_RENDERERS, config, repo)

    assert dto.source.kind == "deployed-artifact"
    assert dto.source.base_ref == "trunk"
    assert dto.preview.startswith(BASE)
    listed = {r.role: r for r in list_roles(repo, config)}
    assert listed["adversarial_review"].source.base_ref == "trunk"


def _launch(tmp_path: Path, repo: Path, head: str) -> tuple[str, str]:
    """Dispatch a review of *head* from *repo* against ``main``; return the
    prompt the launcher received and the attested ``prompt_artifact_sha``."""
    captured: dict[str, ReviewerLaunchContext] = {}

    def launcher(ctx: ReviewerLaunchContext) -> CollectedSidecar:
        captured["ctx"] = ctx
        return CollectedSidecar(ctx.log_dir / "s.json", "d", ParsedVerdict("READY"))

    store = SqliteRunStore(str(tmp_path / "dispatch.db"))
    try:
        result = dispatch_reviewer(
            store,
            DispatchConfig(logging=LoggingConfig(dir=str(tmp_path / "logs"))),
            item_id="42",
            issue_num=42,
            head_sha=head,
            repo_root=repo,
            base_ref="main",
            max_review_usd=5.0,
            run_id="rev-deployed-path",
            node_epic_ref="epic:demo",
            launcher=launcher,
        )
    finally:
        store.close()
    if result.worktree is not None:
        shutil.rmtree(result.worktree, ignore_errors=True)
    assert result.prompt_artifact_sha == captured["ctx"].prompt_artifact_sha
    return captured["ctx"].prompt, result.prompt_artifact_sha


def test_a_committed_symlink_at_the_deployed_path_is_not_the_prompt(
    tmp_path: Path,
) -> None:
    """A symlink's blob is its target's path. The review runs the packaged
    default instead, as when the base ref deploys nothing there."""
    link_target = "../../code.py"
    repo = git_repo(tmp_path / "repo")
    main = commit_reviewer_artifact(repo, link_target, mode="120000")
    default = resolve_reviewer_prompt()  # no repository: the packaged default
    packaged, packaged_sha = default.text, default.sha256

    prompt, attested = _launch(tmp_path, repo, main)

    assert prompt.startswith(packaged)
    assert attested == packaged_sha
    dto = role_preview("adversarial_review", PREVIEW_RENDERERS, None, repo)
    assert dto.source.kind == "pinned-artifact"
    assert dto.source.sha256 == packaged_sha
    assert dto.preview.startswith(packaged)
    assert link_target not in dto.preview.splitlines()[0]


def test_a_tree_listing_git_cannot_read_refuses_rather_than_ship_the_default(
    tmp_path: Path,
) -> None:
    """The tree that would list the deployed copy is gone, so what the base
    ref deploys is unknown: the launch's read and the preview both refuse,
    where a listing that names nothing there loads the packaged default."""
    repo = git_repo(tmp_path / "repo")
    main = commit_reviewer_artifact(repo, BASE)
    lose_object(repo, "main:.claude/kew")

    with pytest.raises(ReviewerPromptReadError, match="tree listing failed"):
        resolve_reviewer_prompt(repo, "main", base_commit=main)
    with pytest.raises(PromptSourceUnreadable):
        role_preview("adversarial_review", PREVIEW_RENDERERS, None, repo)
