"""Tests for the collision-free review checkout (#1290).

Covers the four cases the incident demanded: a clean repo, a DIRTY
implementation checkout holding the PR branch, a reused/detached review
checkout, and two concurrent reviewers of the same PR.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

import pytest
from support.narrow_checkout import (
    assert_narrow_checkout,
    make_narrow_checkout_source,
)

from kew import subprocess_utils
from kew.config import DispatchConfig
from kew.review_checkout import (
    CHECKOUT_BUSY,
    CHECKOUT_DIRTY,
    HEAD_MISMATCH,
    HEAD_UNRESOLVABLE,
    ReviewCheckoutError,
    materialize_review_checkout,
    review_checkout_path,
    worktree_for_branch,
)
from kew.worktree import checkout_existing_worktree

PR_BRANCH = "agent/issue-42"


@pytest.fixture(autouse=True)
def _git_allowed():
    saved = subprocess_utils.TOOL_PATHS.copy()
    subprocess_utils._register_optional_tool("git")
    yield
    subprocess_utils.TOOL_PATHS.clear()
    subprocess_utils.TOOL_PATHS.update(saved)


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _origin_repo(tmp_path: Path, branch: str = PR_BRANCH) -> Path:
    """A bare origin with ``branch`` pushed, plus a detached working clone."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", str(origin)], check=True)
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True)
    _git("config", "user.email", "t@t.t", cwd=work)
    _git("config", "user.name", "t", cwd=work)
    (work / "f.txt").write_text("base")
    _git("add", ".", cwd=work)
    _git("commit", "-qm", "base", cwd=work)
    _git("branch", "-M", "main", cwd=work)
    _git("push", "-q", "origin", "main", cwd=work)
    _git("checkout", "-qb", branch, cwd=work)
    (work / "pr.txt").write_text("pr work")
    _git("add", ".", cwd=work)
    _git("commit", "-qm", "pr work", cwd=work)
    _git("push", "-q", "origin", branch, cwd=work)
    # Detach so the main checkout holds neither branch.
    _git("checkout", "-q", "--detach", cwd=work)
    return work


def _cfg(tmp_path: Path) -> DispatchConfig:
    cfg = DispatchConfig()
    cfg.branches.worktree_base = str(tmp_path / "wt")
    return cfg


def _impl_worktree(work: Path, tmp_path: Path, *, dirty: bool) -> Path:
    """An isolated implementation worktree holding the PR branch."""
    impl = tmp_path / "impl" / "issue-42"
    impl.parent.mkdir(parents=True, exist_ok=True)
    _git("worktree", "add", str(impl), PR_BRANCH, cwd=work)
    if dirty:
        (impl / "in-flight.txt").write_text("half-written implementation")
    return impl


def _head(path: Path) -> str:
    return _git("rev-parse", "HEAD", cwd=path)


def _branch_of(path: Path) -> str:
    return _git("rev-parse", "--abbrev-ref", "HEAD", cwd=path)


class TestWorktreeForBranch:
    def test_finds_the_worktree_holding_the_branch(self, tmp_path, monkeypatch):
        work = _origin_repo(tmp_path)
        monkeypatch.chdir(work)
        impl = _impl_worktree(work, tmp_path, dirty=False)
        assert worktree_for_branch(PR_BRANCH) == impl.resolve()

    def test_none_when_no_worktree_holds_it(self, tmp_path, monkeypatch):
        work = _origin_repo(tmp_path)
        monkeypatch.chdir(work)
        assert worktree_for_branch(PR_BRANCH) is None

    def test_matches_the_exact_branch_not_a_prefix(self, tmp_path, monkeypatch):
        work = _origin_repo(tmp_path)
        monkeypatch.chdir(work)
        _impl_worktree(work, tmp_path, dirty=False)
        # agent/issue-42 is checked out; agent/issue-4 is a prefix, not a match.
        assert worktree_for_branch("agent/issue-4") is None


class TestBranchAlreadyCheckedOut:
    """The #1290 incident: the PR branch lives in another worktree."""

    def test_the_shared_primitive_no_longer_collides(self, tmp_path, monkeypatch):
        """#1290's cause is gone, not merely routed around.

        This asserted a hard failure: ``git worktree add -b`` exited 255 when
        the branch was already checked out elsewhere, which is why reviews grew
        their own checkout. A clone has its own refs, so the same branch can be
        checked out in as many clones as you like — and the implementation
        worktree is untouched either way.
        """
        work = _origin_repo(tmp_path)
        monkeypatch.chdir(work)
        impl = _impl_worktree(work, tmp_path, dirty=True)

        path, _conflict = checkout_existing_worktree(
            issue_num=42, branch_name=PR_BRANCH, config=_cfg(tmp_path)
        )
        assert _branch_of(path) == PR_BRANCH
        assert path != impl
        assert (impl / "in-flight.txt").read_text() == "half-written implementation"

    def test_review_uses_a_detached_checkout(self, tmp_path, monkeypatch):
        work = _origin_repo(tmp_path)
        monkeypatch.chdir(work)
        impl = _impl_worktree(work, tmp_path, dirty=True)

        checkout = materialize_review_checkout(
            pr_number=1288, branch_name=PR_BRANCH, config=_cfg(tmp_path)
        )
        try:
            assert checkout.detached is True
            assert checkout.impl_worktree == impl.resolve()
            assert checkout.path == review_checkout_path(_cfg(tmp_path), 1288)
            assert checkout.path.is_dir()
            # Detached at exactly the PR head — no second local branch.
            assert _branch_of(checkout.path) == "HEAD"
            assert _head(checkout.path) == checkout.head_sha
            assert (checkout.path / "pr.txt").read_text() == "pr work"
        finally:
            checkout.release()

    def test_dirty_implementation_checkout_is_left_untouched(
        self, tmp_path, monkeypatch
    ):
        work = _origin_repo(tmp_path)
        monkeypatch.chdir(work)
        impl = _impl_worktree(work, tmp_path, dirty=True)

        checkout = materialize_review_checkout(
            pr_number=1288, branch_name=PR_BRANCH, config=_cfg(tmp_path)
        )
        try:
            assert checkout.path != impl
            # Not reset, not cleaned, not removed, not shared.
            assert (impl / "in-flight.txt").read_text() == "half-written implementation"
            assert _branch_of(impl) == PR_BRANCH
        finally:
            checkout.release()


class TestCleanRepo:
    def test_creates_a_dedicated_branch_checkout(self, tmp_path, monkeypatch):
        work = _origin_repo(tmp_path)
        monkeypatch.chdir(work)
        cfg = _cfg(tmp_path)

        checkout = materialize_review_checkout(
            pr_number=1288, branch_name=PR_BRANCH, config=cfg
        )
        try:
            assert checkout.detached is False
            assert checkout.impl_worktree is None
            assert checkout.path == review_checkout_path(cfg, 1288)
            assert _branch_of(checkout.path) == PR_BRANCH
            assert _head(checkout.path) == checkout.head_sha
        finally:
            checkout.release()

    def test_never_lands_on_the_implementation_worktree_path(
        self, tmp_path, monkeypatch
    ):
        work = _origin_repo(tmp_path)
        monkeypatch.chdir(work)
        cfg = _cfg(tmp_path)

        checkout = materialize_review_checkout(
            pr_number=1288, branch_name=PR_BRANCH, config=cfg
        )
        try:
            impl_path = Path(cfg.branches.worktree_base) / "agent" / "issue-42"
            assert checkout.path != impl_path
        finally:
            checkout.release()

    def test_integrates_the_base_branch(self, tmp_path, monkeypatch):
        work = _origin_repo(tmp_path)
        monkeypatch.chdir(work)
        # Move main forward so the review checkout must integrate it.
        _git("checkout", "-q", "main", cwd=work)
        (work / "base-moved.txt").write_text("newer base")
        _git("add", ".", cwd=work)
        _git("commit", "-qm", "base moved", cwd=work)
        _git("push", "-q", "origin", "main", cwd=work)
        _git("checkout", "-q", "--detach", cwd=work)

        checkout = materialize_review_checkout(
            pr_number=1288,
            branch_name=PR_BRANCH,
            config=_cfg(tmp_path),
            base_branch="main",
        )
        try:
            assert checkout.conflict is None
            assert (checkout.path / "base-moved.txt").exists()
        finally:
            checkout.release()

    def test_records_the_tree_its_base_integration_left(self, tmp_path, monkeypatch):
        """The tree kew's own merge of the base produced, before any agent runs:
        a remediation that publishes exactly this tree changed nothing, however
        far the merge moved the commit (#3276)."""
        work = _origin_repo(tmp_path)
        monkeypatch.chdir(work)
        pr_tree = _git("rev-parse", f"origin/{PR_BRANCH}^{{tree}}", cwd=work)
        _git("checkout", "-q", "main", cwd=work)
        (work / "base-moved.txt").write_text("newer base")
        _git("add", ".", cwd=work)
        _git("commit", "-qm", "base moved", cwd=work)
        _git("push", "-q", "origin", "main", cwd=work)
        _git("checkout", "-q", "--detach", cwd=work)

        checkout = materialize_review_checkout(
            pr_number=1288,
            branch_name=PR_BRANCH,
            config=_cfg(tmp_path),
            base_branch="main",
        )
        try:
            merged_tree = _git("rev-parse", "HEAD^{tree}", cwd=checkout.path)
            assert checkout.integrated_tree == merged_tree
            assert checkout.integrated_tree != pr_tree
        finally:
            checkout.release()


def _common_git_dir(repo: Path) -> Path:
    """``--git-common-dir`` as an absolute path.

    git answers relatively when you are inside the repository, so resolving the
    bare result would silently anchor it to the *test process's* cwd — which
    monkeypatch has pointed at the operator's checkout, i.e. the exact place
    these tests exist to prove is unreachable.
    """
    raw = Path(_git("rev-parse", "--git-common-dir", cwd=repo))
    return (raw if raw.is_absolute() else repo / raw).resolve()


class TestClonedCheckoutCanCommit:
    """A clone copies objects and refs, never config.

    A repo whose identity is set only locally produces a clone that cannot
    commit at all. On a machine with a global identity this is invisible —
    which is how it reached CI: the suite passed locally and failed on both
    Python legs, where no global identity exists.
    """

    def test_identity_is_inherited_from_the_source_repository(
        self, tmp_path, monkeypatch
    ):
        work = _origin_repo(tmp_path)
        monkeypatch.chdir(work)
        # Reproduce CI: identity exists ONLY in the source repo.
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "absent-gitconfig"))
        monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)

        checkout = materialize_review_checkout(
            pr_number=42, branch_name=PR_BRANCH, config=_cfg(tmp_path)
        )
        try:
            assert _git("config", "--get", "user.email", cwd=checkout.path) == "t@t.t"
            # The property that matters is committing, not the config value.
            (checkout.path / "remediation.txt").write_text("review fix")
            _git("add", "-A", cwd=checkout.path)
            _git("commit", "-m", "remediation", cwd=checkout.path)
        finally:
            checkout.release()


class TestOperatorGitIsUnreachable:
    """W1.1 / D2, doors 2-4: the agent must not reach the operator's ``.git``.

    As a linked worktree this failed on every assertion below. The exploit
    needs no kew dispatch — the planted hook fires on the operator's own
    interactive ``git commit``.
    """

    def test_the_checkout_owns_its_git_directory(self, tmp_path, monkeypatch):
        work = _origin_repo(tmp_path)
        monkeypatch.chdir(work)
        checkout = materialize_review_checkout(
            pr_number=42, branch_name=PR_BRANCH, config=_cfg(tmp_path)
        )
        try:
            dot_git = checkout.path / ".git"
            # A linked worktree's `.git` is a FILE holding `gitdir: <operator>`.
            assert dot_git.is_dir(), f"{dot_git} is not a real repository"
            common = _common_git_dir(checkout.path)
            assert common == dot_git.resolve()
            assert (work / ".git").resolve() not in [common, *common.parents]
        finally:
            checkout.release()

    @pytest.mark.parametrize("packed", [False, True], ids=["loose", "bitmap-pack"])
    def test_only_required_refs_and_objects_reach_the_checkout(
        self, tmp_path, monkeypatch, packed: bool
    ):
        source = make_narrow_checkout_source(tmp_path, packed=packed)
        monkeypatch.chdir(source.repo)
        checkout = materialize_review_checkout(
            pr_number=2026,
            branch_name=source.review_branch,
            base_branch="main",
            config=_cfg(tmp_path),
        )
        try:
            assert_narrow_checkout(
                source,
                checkout.path,
                required_refs=(source.review_sha, "main"),
                expected_refs={
                    f"refs/heads/{source.review_branch}",
                    f"refs/remotes/origin/{source.review_branch}",
                    "refs/remotes/origin/main",
                },
            )
        finally:
            checkout.release()

    def test_the_object_store_is_not_shared_by_inode(self, tmp_path, monkeypatch):
        """Its own ``.git`` means nothing if the objects underneath are shared.

        A local ``git clone`` HARDLINKS object files by default: the clone's
        ``.git/objects/<xx>/<sha>`` is the same inode as the source's. An agent
        can chmod one and write through it, and ``git fsck`` in the OPERATOR's
        repository then reports the object corrupt. Owning the directory is not
        the same as owning the bytes.
        """
        work = _origin_repo(tmp_path)
        monkeypatch.chdir(work)
        checkout = materialize_review_checkout(
            pr_number=42, branch_name=PR_BRANCH, config=_cfg(tmp_path)
        )
        try:
            operator_objects = {
                path.stat().st_ino
                for path in (work / ".git" / "objects").rglob("*")
                if path.is_file()
            }
            assert operator_objects, "fixture has no loose objects to compare"
            shared = [
                path
                for path in (checkout.path / ".git" / "objects").rglob("*")
                if path.is_file() and path.stat().st_ino in operator_objects
            ]
            assert not shared, (
                "the agent's object files are the operator's:\n"
                + "\n".join(f"  {p}" for p in shared)
            )
        finally:
            checkout.release()

    def test_agent_config_writes_cannot_reach_the_operator(self, tmp_path, monkeypatch):
        work = _origin_repo(tmp_path)
        monkeypatch.chdir(work)
        checkout = materialize_review_checkout(
            pr_number=42, branch_name=PR_BRANCH, config=_cfg(tmp_path)
        )
        try:
            # Exactly what a prompt-injected agent would run.
            _git("config", "core.hooksPath", "/tmp/agent", cwd=checkout.path)
            _git("config", "core.fsmonitor", "/tmp/agent/fsmonitor", cwd=checkout.path)
            for key in ("core.hooksPath", "core.fsmonitor"):
                # --local: the agent writes the repository's OWN config, so
                # that is what must stay clean. Without it a developer who sets
                # core.fsmonitor globally would fail this test for a reason
                # that has nothing to do with the agent.
                landed = subprocess.run(
                    ["git", "config", "--local", "--get", key],
                    cwd=work,
                    capture_output=True,
                    text=True,
                )
                assert landed.returncode != 0, (
                    f"{key} reached the operator's config as {landed.stdout.strip()!r}"
                )
        finally:
            checkout.release()

    def test_an_agent_planted_hook_does_not_fire_for_the_operator(
        self, tmp_path, monkeypatch
    ):
        work = _origin_repo(tmp_path)
        monkeypatch.chdir(work)
        checkout = materialize_review_checkout(
            pr_number=42, branch_name=PR_BRANCH, config=_cfg(tmp_path)
        )
        try:
            hooks = _common_git_dir(checkout.path) / "hooks"
            hooks.mkdir(parents=True, exist_ok=True)
            hook = hooks / "pre-commit"
            # The hook's EFFECT is a file, not a line of output. Asserting on
            # captured stdout passed against the vulnerable implementation too
            # — hook output does not reliably land on the stream the caller
            # captures, so the assertion could not fail either way.
            fired = tmp_path / "hook-fired"
            hook.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(fired))}\n")
            hook.chmod(0o755)

            _git("checkout", "-q", "main", cwd=work)
            (work / "operator.txt").write_text("the operator's own work")
            _git("add", "-A", cwd=work)
            _git("commit", "-m", "operator's own work", cwd=work)
            assert not fired.exists(), (
                "an agent-planted hook fired on the operator's own commit"
            )
        finally:
            checkout.release()


class TestReuse:
    def test_clean_review_checkout_is_repointed_to_the_new_head(
        self, tmp_path, monkeypatch
    ):
        work = _origin_repo(tmp_path)
        monkeypatch.chdir(work)
        cfg = _cfg(tmp_path)
        _impl_worktree(work, tmp_path, dirty=True)

        first = materialize_review_checkout(
            pr_number=1288, branch_name=PR_BRANCH, config=cfg
        )
        first.release()

        # The PR head moves (a new push) before the next review round.
        _git("checkout", "-q", PR_BRANCH, cwd=tmp_path / "impl" / "issue-42")
        impl = tmp_path / "impl" / "issue-42"
        _git("add", "-A", cwd=impl)
        _git("commit", "-qm", "more pr work", cwd=impl)
        _git("push", "-q", "origin", PR_BRANCH, cwd=impl)

        second = materialize_review_checkout(
            pr_number=1288, branch_name=PR_BRANCH, config=cfg
        )
        try:
            assert second.path == first.path
            assert second.head_sha != first.head_sha
            assert _head(second.path) == second.head_sha
            assert (second.path / "in-flight.txt").exists()
        finally:
            second.release()

    def test_dirty_review_checkout_is_refused_never_overwritten(
        self, tmp_path, monkeypatch
    ):
        work = _origin_repo(tmp_path)
        monkeypatch.chdir(work)
        cfg = _cfg(tmp_path)

        first = materialize_review_checkout(
            pr_number=1288, branch_name=PR_BRANCH, config=cfg
        )
        first.release()
        (first.path / "reviewer-edit.txt").write_text("unpublished remediation")

        with pytest.raises(ReviewCheckoutError) as excinfo:
            materialize_review_checkout(
                pr_number=1288, branch_name=PR_BRANCH, config=cfg
            )
        assert excinfo.value.classification == CHECKOUT_DIRTY
        assert (first.path / "reviewer-edit.txt").read_text() == (
            "unpublished remediation"
        )


class TestConcurrentReviews:
    def test_second_reviewer_refuses_instead_of_sharing_the_tree(
        self, tmp_path, monkeypatch
    ):
        work = _origin_repo(tmp_path)
        monkeypatch.chdir(work)
        cfg = _cfg(tmp_path)

        held = materialize_review_checkout(
            pr_number=1288, branch_name=PR_BRANCH, config=cfg
        )
        try:
            with pytest.raises(ReviewCheckoutError) as excinfo:
                materialize_review_checkout(
                    pr_number=1288, branch_name=PR_BRANCH, config=cfg
                )
            assert excinfo.value.classification == CHECKOUT_BUSY
        finally:
            held.release()

    def test_claim_is_reusable_after_release(self, tmp_path, monkeypatch):
        work = _origin_repo(tmp_path)
        monkeypatch.chdir(work)
        cfg = _cfg(tmp_path)

        first = materialize_review_checkout(
            pr_number=1288, branch_name=PR_BRANCH, config=cfg
        )
        first.release()
        first.release()  # idempotent

        second = materialize_review_checkout(
            pr_number=1288, branch_name=PR_BRANCH, config=cfg
        )
        second.release()
        assert second.path == first.path

    def test_different_prs_do_not_contend(self, tmp_path, monkeypatch):
        work = _origin_repo(tmp_path)
        monkeypatch.chdir(work)
        cfg = _cfg(tmp_path)
        _impl_worktree(work, tmp_path, dirty=False)

        one = materialize_review_checkout(
            pr_number=1288, branch_name=PR_BRANCH, config=cfg
        )
        two = materialize_review_checkout(
            pr_number=1289, branch_name=PR_BRANCH, config=cfg
        )
        try:
            assert one.path != two.path
            assert one.detached and two.detached
        finally:
            one.release()
            two.release()


class TestHeadBinding:
    def test_mismatched_expected_head_is_refused(self, tmp_path, monkeypatch):
        work = _origin_repo(tmp_path)
        monkeypatch.chdir(work)
        cfg = _cfg(tmp_path)

        with pytest.raises(ReviewCheckoutError) as excinfo:
            materialize_review_checkout(
                pr_number=1288,
                branch_name=PR_BRANCH,
                config=cfg,
                expected_head_sha="0" * 40,
            )
        assert excinfo.value.classification == HEAD_MISMATCH
        # Nothing was materialized — the refusal is genuinely pre-launch.
        assert not review_checkout_path(cfg, 1288).exists()

    def test_matching_expected_head_is_accepted(self, tmp_path, monkeypatch):
        work = _origin_repo(tmp_path)
        monkeypatch.chdir(work)
        head = _git("rev-parse", f"origin/{PR_BRANCH}", cwd=work)

        checkout = materialize_review_checkout(
            pr_number=1288,
            branch_name=PR_BRANCH,
            config=_cfg(tmp_path),
            expected_head_sha=head,
        )
        try:
            assert checkout.head_sha == head
        finally:
            checkout.release()

    def test_unresolvable_head_is_classified(self, tmp_path, monkeypatch):
        work = _origin_repo(tmp_path)
        monkeypatch.chdir(work)

        with pytest.raises(ReviewCheckoutError) as excinfo:
            materialize_review_checkout(
                pr_number=1288, branch_name="agent/never-pushed", config=_cfg(tmp_path)
            )
        assert excinfo.value.classification == HEAD_UNRESOLVABLE


class TestOriginOnlyBranch:
    """#1655: the PR branch exists on origin but nowhere in the operator's repo.

    Every review of somebody else's PR looks like this — the operator never
    checked the branch out, so ``git clone`` of their repository carries no ref
    for it (clone copies ``refs/heads/*``, and the source has none). The review
    clone then had nothing to check out and the whole review refused before it
    launched.
    """

    def test_branch_only_on_origin_is_fetched_into_the_review_clone(
        self, tmp_path, monkeypatch
    ):
        work = _origin_repo(tmp_path)
        head = _git("rev-parse", f"origin/{PR_BRANCH}", cwd=work)
        # The operator has never had this branch locally — only origin has it.
        _git("branch", "-D", PR_BRANCH, cwd=work)
        monkeypatch.chdir(work)

        checkout = materialize_review_checkout(
            pr_number=1655, branch_name=PR_BRANCH, config=_cfg(tmp_path)
        )
        try:
            assert checkout.head_sha == head
            assert not checkout.detached
            assert _branch_of(checkout.path) == PR_BRANCH
            assert _head(checkout.path) == head
            # The fetch lands in the review clone, never in the operator's repo:
            # kew does not add branches to a checkout it does not own.
            assert (
                subprocess.run(
                    ["git", "rev-parse", "--verify", f"refs/heads/{PR_BRANCH}"],
                    cwd=work,
                    capture_output=True,
                ).returncode
                != 0
            )
        finally:
            checkout.release()

    def test_a_branch_on_neither_side_is_still_classified(self, tmp_path, monkeypatch):
        work = _origin_repo(tmp_path)
        monkeypatch.chdir(work)

        with pytest.raises(ReviewCheckoutError) as excinfo:
            materialize_review_checkout(
                pr_number=1655, branch_name="agent/never-pushed", config=_cfg(tmp_path)
            )
        assert excinfo.value.classification == HEAD_UNRESOLVABLE


@pytest.fixture(autouse=True)
def _legacy_checkout_reuse(monkeypatch):
    """These tests document the checkout escape hatch's reuse and refusal
    semantics. Under canonical publication an existing review checkout is moved
    aside intact instead (review round 10 of #1440)."""
    monkeypatch.setattr(
        "kew.worktree.quarantine_agent_checkout", lambda *_a, **_k: None
    )


def test_repoint_refuses_when_the_branch_moved_past_the_recorded_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Audit 2026-10-06 S2, review round 5: the narrow clone carries no source
    # refs, so a non-detached review fetches the PR branch again after kew
    # resolved head_sha. If the PR moved from A to a descendant B in between,
    # the checkout lands on B and the fast-forward to A is a no-op. The
    # reviewer must never run B under A's name.
    from kew import review_checkout

    review_dir = tmp_path / "review"
    review_dir.mkdir()
    _git("init", "-q", "-b", "main", cwd=review_dir)
    _git("config", "user.email", "t@t.t", cwd=review_dir)
    _git("config", "user.name", "t", cwd=review_dir)
    (review_dir / "f.txt").write_text("a\n")
    _git("add", "-A", cwd=review_dir)
    _git("commit", "-q", "-m", "A", cwd=review_dir)
    recorded = _git("rev-parse", "HEAD", cwd=review_dir)
    (review_dir / "f.txt").write_text("b\n")
    _git("commit", "-q", "-am", "B", cwd=review_dir)
    moved = _git("rev-parse", "HEAD", cwd=review_dir)
    _git("update-ref", "refs/remotes/origin/feature", moved, cwd=review_dir)
    _git("checkout", "-q", "--detach", recorded, cwd=review_dir)
    monkeypatch.setattr(review_checkout, "_fetch_origin_only_branch", lambda *_: None)

    with pytest.raises(ReviewCheckoutError) as refused:
        review_checkout._repoint(
            review_dir, branch_name="feature", head_sha=recorded, detached=False
        )

    assert refused.value.classification == HEAD_MISMATCH


def test_a_local_branch_ahead_of_origin_never_becomes_the_review_head(
    tmp_path, monkeypatch
):
    """#1734: the review lands on the head the tracker names, from origin.

    The operator's local branch of the PR's name carries a commit origin has
    never seen. The review checkout is the origin head, and the private commit
    is not even reachable in the review clone.
    """
    work = _origin_repo(tmp_path)
    head = _git("rev-parse", f"origin/{PR_BRANCH}", cwd=work)
    _git("checkout", "-q", PR_BRANCH, cwd=work)
    (work / "private.txt").write_text("operator-only")
    _git("add", ".", cwd=work)
    _git("commit", "-qm", "private", cwd=work)
    private = _git("rev-parse", "HEAD", cwd=work)
    # Ahead of origin and not checked out, as the operator left it.
    _git("checkout", "-q", "--detach", "main", cwd=work)
    assert _git("rev-parse", PR_BRANCH, cwd=work) == private
    monkeypatch.chdir(work)

    checkout = materialize_review_checkout(
        pr_number=1734,
        branch_name=PR_BRANCH,
        config=_cfg(tmp_path),
        expected_head_sha=head,
    )
    try:
        assert checkout.head_sha == head
        assert _head(checkout.path) == head
        assert not (checkout.path / "private.txt").exists()
        reachable = subprocess.run(
            ["git", "cat-file", "-e", f"{private}^{{commit}}"],
            cwd=checkout.path,
            capture_output=True,
        )
        assert reachable.returncode != 0
    finally:
        checkout.release()
