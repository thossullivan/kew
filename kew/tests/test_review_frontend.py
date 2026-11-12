"""The governed reviewer's frontend dependencies (#2559).

A real two-commit repo pins the decision to real git refs: the mount is keyed
by the BASE lockfile, and a PR that changes that lockfile gets no mount and a
prompt line saying why. The container itself is never started; the install's
launch is replaced at ``_run_bounded``, the one seam every provisioning
container goes through.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from typing import Any

import pytest

import kew.review_environment as review_environment
from kew.config import DispatchConfig
from kew.dispatch_sandbox import build_agent_container_argv
from kew.review_contract import CollectedSidecar, ParsedVerdict
from kew.review_dispatch import ReviewerLaunchContext, dispatch_reviewer
from kew.review_environment import Transcript
from kew.review_frontend import (
    CONTAINER_NODE_MODULES,
    CONTAINER_VITE_CACHE,
    plan_frontend_review,
    prepare_frontend_mount,
)
from kew.run_store import SqliteRunStore
from kew.sandbox import build_run_argv
from kew.subprocess_utils import TOOL_PATHS, run

BASE_LOCK = b'{"name": "kew-frontend", "lockfileVersion": 3, "packages": {}}\n'
HEAD_LOCK = b'{"name": "kew-frontend", "packages": {"evil": "file:/x"}}\n'
MANIFEST = b'{"name": "kew-frontend", "private": true}\n'


@pytest.fixture(autouse=True)
def _register_git(monkeypatch: pytest.MonkeyPatch) -> None:
    git = shutil.which("git")
    assert git is not None
    monkeypatch.setitem(TOOL_PATHS, "git", git)


def _git(cwd: Path, *args: str) -> str:
    return run(["git", *args], cwd=cwd, check=True).stdout.strip()


def _repo(tmp_path: Path, *, head_lock: bytes | None) -> tuple[Path, str]:
    """``main`` carries the base lockfile; the returned head may change it."""
    repo = tmp_path / "repo"
    (repo / "frontend").mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "t")
    (repo / "frontend" / "package.json").write_bytes(MANIFEST)
    (repo / "frontend" / "package-lock.json").write_bytes(BASE_LOCK)
    (repo / "frontend" / "App.svelte").write_text("<p>base</p>\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "checkout", "-q", "-b", "pr")
    (repo / "frontend" / "App.svelte").write_text("<p>head</p>\n")
    if head_lock is not None:
        (repo / "frontend" / "package-lock.json").write_bytes(head_lock)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "head")
    return repo, _git(repo, "rev-parse", "HEAD")


def _dispatch(tmp_path: Path, repo: Path, head: str) -> ReviewerLaunchContext:
    captured: dict[str, ReviewerLaunchContext] = {}

    def launcher(ctx: ReviewerLaunchContext) -> CollectedSidecar:
        captured["ctx"] = ctx
        return CollectedSidecar(
            stored_path=ctx.log_dir / "s.json",
            digest="d",
            parsed=ParsedVerdict(verdict="READY"),
        )

    with SqliteRunStore(str(tmp_path / "dispatch.db")) as store:
        result = dispatch_reviewer(
            store,
            DispatchConfig(),
            item_id="42",
            issue_num=42,
            head_sha=head,
            repo_root=repo,
            base_ref="main",
            max_review_usd=5.0,
            run_id="rev-fe",
            node_epic_ref="epic:demo",
            launcher=launcher,
        )
    assert result.worktree is not None
    shutil.rmtree(result.worktree, ignore_errors=True)
    return captured["ctx"]


def test_unchanged_lockfile_is_keyed_by_the_base_lock_hash(tmp_path: Path) -> None:
    repo, head = _repo(tmp_path, head_lock=None)
    ctx = _dispatch(tmp_path, repo, head)

    expected = hashlib.sha256(BASE_LOCK).hexdigest()
    assert ctx.frontend_lock_sha256 == expected
    assert f"sha256 `{expected}`" in ctx.prompt
    assert "npx --offline vitest run" in ctx.prompt
    assert "npx --offline svelte-check" in ctx.prompt


def test_a_pr_that_changes_the_lockfile_gets_no_mount_and_says_why(
    tmp_path: Path,
) -> None:
    repo, head = _repo(tmp_path, head_lock=HEAD_LOCK)
    ctx = _dispatch(tmp_path, repo, head)

    assert ctx.frontend_lock_sha256 is None
    assert "This PR changes `frontend/package-lock.json`" in ctx.prompt
    assert "mounted no\n`frontend/node_modules`" in ctx.prompt
    # And the launcher, handed that context, mounts nothing and builds nothing.
    checkout = tmp_path / "checkout"
    (checkout / "frontend").mkdir(parents=True)
    mounts = prepare_frontend_mount(
        DispatchConfig(),
        image="img",
        runner="claude-code",
        checkout_dir=checkout,
        slug="42",
        lock_sha256=ctx.frontend_lock_sha256,
        base_ref="main",
        cache_root=tmp_path / "cache",
    )
    assert mounts == ()
    assert not (tmp_path / "cache").exists()
    argv = build_agent_container_argv(
        DispatchConfig(),
        image="img",
        runner="claude-code",
        checkout_dir=checkout,
        slug="42",
        model="",
        inference_transport=None,
        read_only_mounts=mounts,
    )
    assert not any("node_modules" in arg for arg in argv)


def test_a_repo_without_a_frontend_lockfile_gets_nothing(tmp_path: Path) -> None:
    repo = tmp_path / "plain"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "t")
    (repo / "x.py").write_text("x = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")

    plan = plan_frontend_review(repo, "HEAD", "HEAD")
    assert plan.lock_sha256 is None
    assert plan.section == ""


def _reviewer_checkout(tmp_path: Path, repo: Path, head: str) -> Path:
    """A clone at *head* carrying the reviewer's pinned base ref."""
    checkout = tmp_path / "review"
    _git(tmp_path, "clone", "-q", str(repo), str(checkout))
    _git(
        checkout, "update-ref", "refs/kew/review/base", _git(repo, "rev-parse", "main")
    )
    _git(checkout, "checkout", "-q", "--detach", head)
    return checkout


def test_reviewer_container_spec_mounts_the_cached_tree_read_only(
    tmp_path: Path,
) -> None:
    """AC1: the reviewer's argv binds the tree keyed by the base lock hash."""
    repo, head = _repo(tmp_path, head_lock=None)
    checkout = _reviewer_checkout(tmp_path, repo, head)
    sha = hashlib.sha256(BASE_LOCK).hexdigest()
    cache = tmp_path / "cache"
    (cache / sha).mkdir(parents=True)

    mounts = prepare_frontend_mount(
        DispatchConfig(),
        image="img",
        runner="claude-code",
        checkout_dir=checkout,
        slug="42",
        lock_sha256=sha,
        base_ref="refs/kew/review/base",
        cache_root=cache,
    )
    argv = build_agent_container_argv(
        DispatchConfig(),
        image="img",
        runner="claude-code",
        checkout_dir=checkout,
        slug="42",
        model="",
        inference_transport=None,
        read_only_mounts=mounts,
    )

    bind = f"{(cache / sha).absolute()}:{CONTAINER_NODE_MODULES}:ro"
    assert argv[argv.index(bind) - 1] == "-v"
    # The mount point and Vite's writable cache exist, empty, in the checkout.
    assert (checkout / "frontend" / "node_modules").is_dir()
    assert not any((checkout / "frontend" / "node_modules").iterdir())
    assert (checkout / ".kew-vite-cache").is_dir()


def test_a_missing_cache_is_built_from_the_base_lockfile_without_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, head = _repo(tmp_path, head_lock=None)
    checkout = _reviewer_checkout(tmp_path, repo, head)
    # Even if the checkout's working copy says otherwise, the base ref decides.
    (checkout / "frontend" / "package-lock.json").write_bytes(HEAD_LOCK)
    sha = hashlib.sha256(BASE_LOCK).hexdigest()
    seen: dict[str, Any] = {}

    def fake_run_bounded(argv: list[str], mount: Path, *_a: Any, **_k: Any):
        seen["argv"] = argv
        seen["lock"] = (mount / "package-lock.json").read_bytes()
        (mount / "node_modules" / "vitest").mkdir(parents=True)
        return Transcript(0, "", [])

    monkeypatch.setattr(review_environment, "_run_bounded", fake_run_bounded)
    cache = tmp_path / "cache"
    mounts = prepare_frontend_mount(
        DispatchConfig(),
        image="img",
        runner="claude-code",
        checkout_dir=checkout,
        slug="42",
        lock_sha256=sha,
        base_ref="refs/kew/review/base",
        cache_root=cache,
    )

    assert mounts == (((cache / sha).absolute(), CONTAINER_NODE_MODULES),)
    assert seen["lock"] == BASE_LOCK
    argv = seen["argv"]
    assert argv[argv.index("--entrypoint") + 1] == "npm"
    assert argv[-4:] == ["ci", "--ignore-scripts", "--no-audit", "--no-fund"]
    assert not any(arg.startswith("CLAUDE_CODE") for arg in argv)
    assert (cache / sha / "vitest").is_dir()
    assert (cache / sha / ".vite").readlink() == Path(CONTAINER_VITE_CACHE)
    # The scratch build directory is gone; only the published tree remains.
    assert [p.name for p in cache.iterdir()] == [sha]


def test_a_failed_install_mounts_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, head = _repo(tmp_path, head_lock=None)
    checkout = _reviewer_checkout(tmp_path, repo, head)
    monkeypatch.setattr(
        review_environment,
        "_run_bounded",
        lambda *_a, **_k: Transcript(1, "", ["npm ERR! network"]),
    )
    cache = tmp_path / "cache"
    mounts = prepare_frontend_mount(
        DispatchConfig(),
        image="img",
        runner="claude-code",
        checkout_dir=checkout,
        slug="42",
        lock_sha256=hashlib.sha256(BASE_LOCK).hexdigest(),
        base_ref="refs/kew/review/base",
        cache_root=cache,
    )
    assert mounts == ()
    assert list(cache.iterdir()) == []


def test_a_planted_node_modules_symlink_is_never_mounted_over(
    tmp_path: Path,
) -> None:
    repo, head = _repo(tmp_path, head_lock=None)
    checkout = _reviewer_checkout(tmp_path, repo, head)
    (checkout / "frontend" / "node_modules").symlink_to("/etc")
    sha = hashlib.sha256(BASE_LOCK).hexdigest()
    (tmp_path / "cache" / sha).mkdir(parents=True)
    mounts = prepare_frontend_mount(
        DispatchConfig(),
        image="img",
        runner="claude-code",
        checkout_dir=checkout,
        slug="42",
        lock_sha256=sha,
        base_ref="refs/kew/review/base",
        cache_root=tmp_path / "cache",
    )
    assert mounts == ()


@pytest.mark.parametrize(
    "mount",
    [
        (Path("/cache/x"), "/etc"),
        (Path("/cache/x"), "/work/../etc"),
        (Path("relative"), "/work/frontend/node_modules"),
        (Path("/cache/x:rw"), "/work/frontend/node_modules"),
    ],
)
def test_build_run_argv_refuses_an_unsafe_read_only_mount(
    mount: tuple[Path, str],
) -> None:
    with pytest.raises(ValueError):
        build_run_argv(
            runtime="docker",
            image="img",
            checkout_dir=Path("/tmp/x"),  # nosec B108 - argv only, never created
            slug="s",
            network="none",
            runner="claude-code",
            read_only_mounts=(mount,),
        )
