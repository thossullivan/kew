"""Tests for reviewer dispatch (Task 6) + head-ref context masking (Task 7).

The masking tests use a real two-commit git repo (base clean, head poisoned) so
the invariant is pinned against real git materialization, per the
``concurrent-state-change`` skill's "real store / real materialization" rule.
"""

import hashlib
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from support.narrow_checkout import (
    assert_narrow_checkout,
    make_narrow_checkout_source,
)
from support.reviewer_artifact import commit_reviewer_artifact

from kew.config import DispatchConfig
from kew.review_contract import CollectedSidecar, ParsedVerdict
from kew.review_dispatch import (
    REVIEW_EPIC_REF,
    ReviewerDispatch,
    ReviewerLaunchContext,
    ReviewerLaunchError,
    dispatch_reviewer,
    mask_head_ref_context,
    materialize_reviewer_worktree,
    reviewer_received_prompt,
)
from kew.run_store import ADVERSARIAL_REVIEW_RUN_KIND, RunRecord, SqliteRunStore
from kew.subprocess_utils import run

MARKER = "MALICIOUS-STEER-INSTRUCTION-9f3a"


@pytest.fixture(autouse=True)
def _register_git(monkeypatch):
    import shutil

    from kew.subprocess_utils import TOOL_PATHS

    git = shutil.which("git")
    assert git is not None, "git required for review-dispatch tests"
    monkeypatch.setitem(TOOL_PATHS, "git", git)


def _git(cwd: Path, *args: str) -> None:
    run(["git", *args], cwd=cwd, check=True)


def _make_repo(tmp_path: Path) -> tuple[Path, str, str]:
    """Base commit (clean auto-load files) + head commit (marker-poisoned).

    Returns ``(repo_root, base_sha, head_sha)``.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "t")
    # Base: clean auto-load surface, at the root AND nested (a co-located nested
    # file exercises base restoration at depth).
    (repo / "CLAUDE.md").write_text("# base project context\n")
    (repo / "CLAUDE.local.md").write_text("base local\n")
    (repo / ".mcp.json").write_text("{}\n")
    (repo / ".claude").mkdir()
    (repo / ".claude" / "settings.json").write_text('{"base": true}\n')
    (repo / "src").mkdir()
    (repo / "src" / "CLAUDE.md").write_text("# base src context\n")
    (repo / "AGENTS.md").write_text("base agents\n")
    (repo / "code.py").write_text("x = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    base_sha = run(["git", "rev-parse", "HEAD"], cwd=repo, check=True).stdout.strip()
    # Head: poison every root auto-load file AND plant nested/new steering
    # surfaces a PR author could add — nested CLAUDE.md, a nested .claude dir, and
    # AGENTS.md (OpenCode runner). Each is auto-loaded as instructions if it
    # reaches the reviewer (#979-F1). Some are head-only (no base counterpart).
    (repo / "CLAUDE.md").write_text(f"# {MARKER}\n")
    (repo / "CLAUDE.local.md").write_text(f"{MARKER}\n")
    (repo / ".mcp.json").write_text(f'{{"note": "{MARKER}"}}\n')
    (repo / ".claude" / "settings.json").write_text(f'{{"steer": "{MARKER}"}}\n')
    (repo / ".claude" / "evil.md").write_text(f"{MARKER}\n")
    # Nested co-located file (base has src/CLAUDE.md → base must be restored).
    (repo / "src" / "CLAUDE.md").write_text(f"# {MARKER}\n")
    # Root AGENTS.md co-located with base → base must be restored.
    (repo / "AGENTS.md").write_text(f"{MARKER}\n")
    # Head-only nested surfaces (no base counterpart → must be gone entirely).
    (repo / "pkg").mkdir()
    (repo / "pkg" / ".claude").mkdir()
    (repo / "pkg" / ".claude" / "settings.json").write_text(f'{{"x": "{MARKER}"}}\n')
    (repo / "deep" / "nested").mkdir(parents=True)
    (repo / "deep" / "nested" / "CLAUDE.md").write_text(f"# {MARKER}\n")
    (repo / "code.py").write_text("x = 2  # the actual PR change\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "head")
    head_sha = run(["git", "rev-parse", "HEAD"], cwd=repo, check=True).stdout.strip()
    return repo, base_sha, head_sha


@pytest.mark.parametrize("packed", [False, True], ids=["loose", "bitmap-pack"])
def test_reviewer_checkout_excludes_unrelated_and_unreachable_objects(
    tmp_path: Path, packed: bool
) -> None:
    import shutil

    from kew.worktree import release_checkout_claim

    source = make_narrow_checkout_source(tmp_path, packed=packed)
    checkout = materialize_reviewer_worktree(
        source.repo, source.review_sha, base_ref="main"
    )
    try:
        assert_narrow_checkout(
            source,
            checkout,
            required_refs=("main", source.review_sha),
            expected_refs={"refs/kew/review/base", "refs/kew/review/head"},
        )
    finally:
        release_checkout_claim(checkout)
        shutil.rmtree(checkout, ignore_errors=True)


def test_reviewer_bootstraps_locally_cached_head_without_origin(
    tmp_path: Path,
) -> None:
    import shutil

    from kew.worktree import release_checkout_claim

    source = make_narrow_checkout_source(tmp_path)
    _git(source.repo, "switch", "-q", "-c", "cached-review-head")
    (source.repo / "cached.txt").write_text("cached PR head\n")
    _git(source.repo, "add", "cached.txt")
    _git(source.repo, "commit", "-q", "-m", "cached head")
    head_sha = run(
        ["git", "rev-parse", "HEAD"], cwd=source.repo, check=True
    ).stdout.strip()
    _git(source.repo, "switch", "-q", "main")
    assert (
        run(
            ["git", "ls-remote", "--heads", "origin", "cached-review-head"],
            cwd=source.repo,
            check=True,
        ).stdout.strip()
        == ""
    )

    checkout = materialize_reviewer_worktree(
        source.repo, head_sha, base_ref=source.main_sha
    )
    try:
        assert_narrow_checkout(
            source,
            checkout,
            required_refs=(source.main_sha, head_sha),
            expected_refs={"refs/kew/review/base", "refs/kew/review/head"},
        )
    finally:
        release_checkout_claim(checkout)
        shutil.rmtree(checkout, ignore_errors=True)


# --- Task 7: head-ref context masking (F12/N5) ---


@pytest.mark.parametrize("contained", [True, False])
def test_marker_never_reaches_reviewer_context(tmp_path: Path, contained: bool):
    """A marker planted in any head-ref auto-load surface — at the root OR nested
    at any depth — must not appear in the reviewer's effective context, for both
    the contained and uncontained path (both run the identical materialization +
    masking helper). Nested surfaces are the load-bearing #979-F1 case: Claude
    Code auto-loads a subdirectory CLAUDE.md as instructions, and OpenCode
    auto-loads AGENTS.md."""
    repo, base_sha, head_sha = _make_repo(tmp_path)
    worktree = materialize_reviewer_worktree(repo, head_sha, base_ref=base_sha)

    # The actual PR change survives (the reviewer reviews the diff).
    assert "the actual PR change" in (worktree / "code.py").read_text()
    # No root auto-load file carries the marker.
    for rel in ("CLAUDE.md", "CLAUDE.local.md", ".mcp.json", "AGENTS.md"):
        assert MARKER not in (worktree / rel).read_text()
    assert MARKER not in (worktree / ".claude" / "settings.json").read_text()
    # No NESTED auto-load file carries the marker (co-located src/CLAUDE.md is
    # restored from base; the AGENTS.md/root are restored too).
    assert MARKER not in (worktree / "src" / "CLAUDE.md").read_text()
    assert "base src context" in (worktree / "src" / "CLAUDE.md").read_text()
    # Head-only auto-load surfaces — root, nested dir, and deep — are gone.
    assert not (worktree / ".claude" / "evil.md").exists()
    assert not (worktree / "pkg" / ".claude").exists()
    assert not (worktree / "deep" / "nested" / "CLAUDE.md").exists()
    # Base content is what remains at the root.
    assert "base project context" in (worktree / "CLAUDE.md").read_text()
    assert "base agents" in (worktree / "AGENTS.md").read_text()


def test_materialize_fetches_a_head_that_exists_only_on_the_remote(tmp_path: Path):
    """#1004: the recorded head_sha is a PR head the implementation agent pushed
    to origin (GitHub) — NOT present in repo_root's local clone. materialize must
    fetch that sha from repo_root's origin so the detach checkout can reach it,
    never a `git checkout` 128. The local-head tests above structurally mask this
    (their head_sha is repo_root's own commit); the first live drive hit it."""
    # origin = the GitHub stand-in (bare); repo_root clones it with a base commit.
    origin = tmp_path / "origin.git"
    run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    repo = tmp_path / "repo_root"
    run(["git", "clone", "-q", str(origin), str(repo)], check=True)
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "t")
    (repo / "code.py").write_text("x = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "push", "-q", "origin", "main")
    base_sha = run(["git", "rev-parse", "HEAD"], cwd=repo, check=True).stdout.strip()

    # A separate "agent" clone pushes a PR branch whose head commit repo_root has
    # never fetched — the exact Bug C precondition.
    agent = tmp_path / "agent"
    run(["git", "clone", "-q", str(origin), str(agent)], check=True)
    _git(agent, "config", "user.email", "a@a.a")
    _git(agent, "config", "user.name", "a")
    (agent / "code.py").write_text("x = 2  # the actual PR change\n")
    _git(agent, "add", "-A")
    _git(agent, "commit", "-q", "-m", "pr work")
    head_sha = run(["git", "rev-parse", "HEAD"], cwd=agent, check=True).stdout.strip()
    _git(agent, "push", "-q", "origin", "HEAD:refs/heads/agent/issue-996")

    # Precondition: repo_root does NOT have the PR head (would make checkout 128).
    assert (
        run(["git", "cat-file", "-e", head_sha], cwd=repo, check=False).returncode != 0
    )

    # Must succeed — materialize fetches the head from repo_root's origin.
    worktree = materialize_reviewer_worktree(repo, head_sha, base_ref=base_sha)
    assert "the actual PR change" in (worktree / "code.py").read_text()


def test_mask_removes_head_only_file_when_base_lacks_it(tmp_path: Path):
    repo, base_sha, head_sha = _make_repo(tmp_path)
    # Remove CLAUDE.local.md at base so masking has nothing to restore.
    _git(repo, "rm", "-q", "CLAUDE.local.md")
    _git(repo, "commit", "-q", "-m", "drop local at base")
    new_base = run(["git", "rev-parse", "HEAD"], cwd=repo, check=True).stdout.strip()
    # Head keeps the poisoned file (branch off the poisoned head from _make_repo).
    worktree = materialize_reviewer_worktree(repo, head_sha, base_ref=new_base)
    # base_sha unused for this scenario; head still had CLAUDE.local.md poisoned.
    assert base_sha  # sanity
    assert not (worktree / "CLAUDE.local.md").exists()


def test_mask_is_idempotent_on_clean_checkout(tmp_path: Path):
    repo, base_sha, head_sha = _make_repo(tmp_path)
    worktree = materialize_reviewer_worktree(repo, head_sha, base_ref=base_sha)
    masked_again = mask_head_ref_context(worktree, base_ref=base_sha)
    assert "CLAUDE.md" in masked_again  # still present from base, still masked


def test_invalid_base_ref_fails_before_masking_head_context(tmp_path: Path):
    repo, _base_sha, head_sha = _make_repo(tmp_path)
    _git(repo, "checkout", "--detach", head_sha)

    before = (repo / "CLAUDE.md").read_text()
    with pytest.raises(subprocess.CalledProcessError):
        mask_head_ref_context(repo, base_ref="refs/kew/review/missing")

    assert (repo / "CLAUDE.md").read_text() == before
    assert MARKER in before


def test_mask_staging_failure_leaves_all_auto_load_paths_untouched(
    tmp_path: Path, monkeypatch
):
    repo, base_sha, head_sha = _make_repo(tmp_path)
    _git(repo, "checkout", "--detach", head_sha)
    before = {
        rel: (repo / rel).read_text()
        for rel in ("CLAUDE.md", "CLAUDE.local.md", "AGENTS.md")
    }
    real_run = run

    def fail_checkout_index(cmd, **kwargs):
        if cmd[:2] == ["git", "checkout-index"]:
            raise subprocess.CalledProcessError(128, cmd)
        return real_run(cmd, **kwargs)

    monkeypatch.setattr("kew.review_dispatch.run", fail_checkout_index)
    with pytest.raises(subprocess.CalledProcessError):
        mask_head_ref_context(repo, base_ref=base_sha)

    assert {rel: (repo / rel).read_text() for rel in before} == before


# --- Task 6: reviewer dispatch ---


def _store(tmp_path: Path) -> SqliteRunStore:
    return SqliteRunStore(str(tmp_path / "dispatch.db"))


def _config() -> DispatchConfig:
    return DispatchConfig()


def test_sha_recorded_and_bound(tmp_path: Path):
    """The worktree is cut at the recorded head SHA, and that sha is bound into
    the dispatch result + the launch context handed to the reviewer (F1)."""
    repo, _base, head_sha = _make_repo(tmp_path)
    captured: dict[str, ReviewerLaunchContext] = {}

    def launcher(ctx: ReviewerLaunchContext) -> CollectedSidecar:
        captured["ctx"] = ctx
        return CollectedSidecar(
            stored_path=ctx.log_dir / "s.json",
            digest="d",
            parsed=ParsedVerdict(verdict="READY"),
        )

    with _store(tmp_path) as store:
        result = dispatch_reviewer(
            store,
            _config(),
            item_id="42",
            issue_num=42,
            head_sha=head_sha,
            repo_root=repo,
            base_ref="main",
            max_review_usd=5.0,
            run_id="rev-run-1",
            node_epic_ref="epic:demo",
            launcher=launcher,
        )

    assert result.outcome == "dispatched"
    assert result.head_sha == head_sha
    assert captured["ctx"].head_sha == head_sha
    # The worktree HEAD really is the recorded sha (detached).
    wt_head = run(
        ["git", "rev-parse", "HEAD"], cwd=result.worktree, check=True
    ).stdout.strip()
    assert wt_head == head_sha


def test_reviewer_prompt_names_pinned_base_and_head_diff_boundary(tmp_path: Path):
    repo, _base, head_sha = _make_repo(tmp_path)
    captured: dict[str, ReviewerLaunchContext] = {}

    def launcher(ctx: ReviewerLaunchContext) -> CollectedSidecar:
        captured["ctx"] = ctx
        return CollectedSidecar(
            ctx.log_dir / "s.json", "d", ParsedVerdict(verdict="READY")
        )

    with _store(tmp_path) as store:
        dispatch_reviewer(
            store,
            _config(),
            item_id="42",
            issue_num=42,
            head_sha=head_sha,
            repo_root=repo,
            base_ref="main",
            max_review_usd=5.0,
            run_id="rev-prompt-boundary",
            node_epic_ref="epic:demo",
            launcher=launcher,
        )

    prompt = captured["ctx"].prompt
    assert (
        "git diff --no-ext-diff --no-textconv --text --ignore-submodules=none "
        "--no-color refs/kew/review/base...refs/kew/review/head --"
    ) in prompt
    assert "git status" in prompt
    assert "do not define PR scope" in prompt


def _prompt_diff_argv(prompt: str) -> list[str]:
    """The `git diff` command line the prompt tells the reviewer to run."""
    lines = [ln.strip() for ln in prompt.splitlines()]
    command = next(ln for ln in lines if ln.startswith("git diff "))
    return command.split()


def test_prompt_diff_shows_what_head_attributes_and_gitmodules_hide(tmp_path: Path):
    # Audit 2026-10-06 R4: the reviewer checkout sits at the PR head, so git
    # reads the PR's own .gitattributes and .gitmodules. `*.py -diff` would
    # print "Binary files differ" for every changed file, and a submodule with
    # `ignore = all` would drop its gitlink change from the diff entirely.
    repo, _base, head_sha = _make_repo(tmp_path)
    captured: dict[str, ReviewerLaunchContext] = {}

    def launcher(ctx: ReviewerLaunchContext) -> CollectedSidecar:
        captured["ctx"] = ctx
        return CollectedSidecar(
            ctx.log_dir / "s.json", "d", ParsedVerdict(verdict="READY")
        )

    with _store(tmp_path) as store:
        dispatch_reviewer(
            store,
            _config(),
            item_id="42",
            issue_num=42,
            head_sha=head_sha,
            repo_root=repo,
            base_ref="main",
            max_review_usd=5.0,
            run_id="rev-prompt-attributes",
            node_epic_ref="epic:demo",
            launcher=launcher,
        )
    argv = _prompt_diff_argv(captured["ctx"].prompt)

    hidden = tmp_path / "hidden"
    hidden.mkdir()
    _git(hidden, "init", "-q", "-b", "main")
    _git(hidden, "config", "user.email", "t@t.t")
    _git(hidden, "config", "user.name", "t")
    (hidden / "code.py").write_text("x = 1\n")
    _git(hidden, "add", "-A")
    _git(hidden, "commit", "-q", "-m", "base")
    _git(hidden, "update-ref", "refs/kew/review/base", "HEAD")
    (hidden / "code.py").write_text('x = "smuggled"\n')
    (hidden / ".gitattributes").write_text("*.py -diff\n")
    gitlink = "1" * 40
    _git(hidden, "update-index", "--add", "--cacheinfo", f"160000,{gitlink},vendor")
    (hidden / ".gitmodules").write_text(
        '[submodule "vendor"]\n\tpath = vendor\n\turl = ./vendor\n\tignore = all\n'
    )
    _git(hidden, "add", "code.py", ".gitattributes", ".gitmodules")
    _git(hidden, "commit", "-q", "-m", "head")
    _git(hidden, "update-ref", "refs/kew/review/head", "HEAD")

    plain = run(
        ["git", "diff", "refs/kew/review/base...refs/kew/review/head", "--"],
        cwd=hidden,
        check=True,
    ).stdout
    # Both attacks work on a bare `git diff`.
    assert "Binary files" in plain
    assert gitlink not in plain
    shown = run(argv, cwd=hidden, check=True).stdout
    assert '+x = "smuggled"' in shown
    assert "Binary files" not in shown
    assert f"+Subproject commit {gitlink}" in shown


def test_reviewer_prompt_names_review_budget_guard(tmp_path: Path):
    repo, _base, head_sha = _make_repo(tmp_path)
    captured: dict[str, ReviewerLaunchContext] = {}

    def launcher(ctx: ReviewerLaunchContext) -> CollectedSidecar:
        captured["ctx"] = ctx
        return CollectedSidecar(ctx.log_dir / "s", "d", ParsedVerdict("READY"))

    with _store(tmp_path) as store:
        dispatch_reviewer(
            store,
            _config(),
            item_id="42",
            issue_num=42,
            head_sha=head_sha,
            repo_root=repo,
            base_ref="main",
            max_review_usd=5.0,
            run_id="rev-prompt-budget",
            node_epic_ref="epic:demo",
            launcher=launcher,
        )

    prompt = captured["ctx"].prompt
    assert "$5.00" in prompt
    assert "offline" in prompt.lower()
    assert "acceptance_executed: false" in prompt
    assert "stop investigating" in prompt.lower()
    assert "end with your verdict block" in prompt.lower()


def test_reviewer_prompt_forbids_background_tasks_even_when_pinned_copy_is_stale(
    tmp_path: Path,
):
    """The background-task ban is host-appended, not only pinned (#2118).

    A reviewer background task re-invokes the reviewer when it finishes, and the
    extra ``result`` event that emits after the verdict makes the round
    unparseable. A repo whose base ref still carries a pre-#2118 copy of the
    pinned prompt must still be told, so the rule rides the host-owned guidance.
    """
    repo, _base, head_sha = _make_repo(tmp_path)
    commit_reviewer_artifact(
        repo, "---\nname: adversarial-reviewer\n---\nold pinned prompt\n"
    )
    captured: dict[str, ReviewerLaunchContext] = {}

    def launcher(ctx: ReviewerLaunchContext) -> CollectedSidecar:
        captured["ctx"] = ctx
        return CollectedSidecar(ctx.log_dir / "s", "d", ParsedVerdict("READY"))

    with _store(tmp_path) as store:
        dispatch_reviewer(
            store,
            _config(),
            item_id="42",
            issue_num=42,
            head_sha=head_sha,
            repo_root=repo,
            base_ref="main",
            max_review_usd=5.0,
            run_id="rev-prompt-foreground",
            node_epic_ref="epic:demo",
            launcher=launcher,
        )

    prompt = captured["ctx"].prompt.lower()
    assert "old pinned prompt" in prompt  # the stale base-ref copy really won
    assert "do not start background tasks" in prompt
    assert "foreground" in prompt
    assert "result event" in prompt


def test_effective_prompt_sha_includes_host_diff_boundary(tmp_path: Path):
    repo, _base, head_sha = _make_repo(tmp_path)
    captured: dict[str, ReviewerLaunchContext] = {}

    def launcher(ctx: ReviewerLaunchContext) -> CollectedSidecar:
        captured["ctx"] = ctx
        return CollectedSidecar(
            ctx.log_dir / "s.json", "d", ParsedVerdict(verdict="READY")
        )

    with _store(tmp_path) as store:
        result = dispatch_reviewer(
            store,
            _config(),
            item_id="42",
            issue_num=42,
            head_sha=head_sha,
            repo_root=repo,
            base_ref="main",
            max_review_usd=5.0,
            run_id="rev-prompt-sha",
            node_epic_ref="epic:demo",
            launcher=launcher,
        )

    final_prompt = captured["ctx"].prompt
    assert "refs/kew/review/base...refs/kew/review/head" in final_prompt
    received = reviewer_received_prompt(final_prompt, head_sha)
    expected = hashlib.sha256(received.encode()).hexdigest()
    assert result.effective_prompt_sha == expected


def test_reservation_parks_under_review_pseudo_epic(tmp_path: Path):
    repo, _base, head_sha = _make_repo(tmp_path)

    def launcher(ctx: ReviewerLaunchContext) -> CollectedSidecar:
        # While the reviewer is live, its reservation must be under the review
        # pseudo-epic — NOT on the node's item epic (would derive IN_PROGRESS).
        review_res = ctx  # noqa: F841
        return CollectedSidecar(ctx.log_dir / "s", "d", ParsedVerdict("READY"))

    with _store(tmp_path) as store:
        dispatch_reviewer(
            store,
            _config(),
            item_id="42",
            issue_num=42,
            head_sha=head_sha,
            repo_root=repo,
            base_ref="main",
            max_review_usd=5.0,
            run_id="rev-run-2",
            node_epic_ref="epic:demo",
            launcher=launcher,
        )
        reservations = store.list_epic_reservations(REVIEW_EPIC_REF)
        assert [r.item_id for r in reservations] == ["42"]
        assert store.list_epic_reservations("epic:demo") == []


def test_budget_refusal_parity(tmp_path: Path):
    """Review-pool refusal mirrors dispatch_core: reservation released, terminal
    refused RunRecord written, node HELD with a review_budget reason, audit
    event emitted — and NO reviewer launched."""
    repo, _base, head_sha = _make_repo(tmp_path)
    now = datetime(2026, 7, 23, tzinfo=UTC)
    launched = False

    def launcher(ctx: ReviewerLaunchContext) -> CollectedSidecar:
        nonlocal launched
        launched = True
        return CollectedSidecar(ctx.log_dir / "s", "d", ParsedVerdict("READY"))

    with _store(tmp_path) as store:
        # Sink prior review spend at the cap so the next reservation refuses.
        store.save(
            RunRecord(
                run_id="prior-review",
                issue_num=42,
                item_id="42",
                branch="agent/issue-42",
                status="success",
                exit_code=0,
                started_at=now - timedelta(minutes=5),
                finished_at=now - timedelta(minutes=4),
                estimated_cost_usd=6.0,
                run_kind=ADVERSARIAL_REVIEW_RUN_KIND,
            )
        )
        result = dispatch_reviewer(
            store,
            _config(),
            item_id="42",
            issue_num=42,
            head_sha=head_sha,
            repo_root=repo,
            base_ref="main",
            max_review_usd=5.0,
            run_id="rev-run-3",
            node_epic_ref="epic:demo",
            blocking=True,
            launcher=launcher,
            now=now,
        )

        assert result.outcome == "budget_refused"
        assert launched is False
        # Reservation released.
        assert store.list_epic_reservations(REVIEW_EPIC_REF) == []
        # Terminal refusal row keyed to the reservation run_id.
        refusal = next(r for r in store.list_by_item("42") if r.run_id == "rev-run-3")
        assert refusal.status == "refused"
        assert refusal.run_kind == ADVERSARIAL_REVIEW_RUN_KIND
        # Node HELD with a routable review_budget reason.
        holds = {h.item_id: h.reason for h in store.list_epic_holds("epic:demo")}
        assert "42" in holds
        assert holds["42"].startswith("review_budget")


def test_launcher_raise_removes_temp_checkout(tmp_path: Path):
    """A launcher that raises must not leak the materialized temp checkout.

    The raise reaches the caller as a ``ReviewerLaunchError`` carrying the
    prompt the reviewer ran under (#3024), the launcher's own error its cause."""
    repo, _base, head_sha = _make_repo(tmp_path)
    captured: dict[str, Path] = {}

    def launcher(ctx: ReviewerLaunchContext) -> CollectedSidecar:
        captured["worktree"] = ctx.worktree
        raise RuntimeError("launcher blew up")

    with (
        _store(tmp_path) as store,
        pytest.raises(ReviewerLaunchError) as raised,
    ):
        dispatch_reviewer(
            store,
            _config(),
            item_id="42",
            issue_num=42,
            head_sha=head_sha,
            repo_root=repo,
            base_ref="main",
            max_review_usd=5.0,
            run_id="rev-run-5",
            node_epic_ref="epic:demo",
            launcher=launcher,
        )

    assert not captured["worktree"].exists()
    assert isinstance(raised.value.__cause__, RuntimeError)
    assert str(raised.value.__cause__) == "launcher blew up"
    assert raised.value.provenance.prompt_artifact_sha is not None
    assert raised.value.provenance.effective_prompt_sha is not None


def test_materialize_failure_removes_temp_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A materialization failure (bad head sha) must not leak the temp checkout.

    Observed in a temp root this test owns, not the machine's. Globbing the
    shared root made the assertion sensitive to anything else on the host
    creating a ``kew-review-*`` while it ran — a concurrent real review on the
    CI runner would fail it, and the ~3,100-entry set difference buried the
    finding in its own output (#1480). An empty directory it created itself is
    both a stronger claim and a one-line diff.
    """
    repo, _base, _head = _make_repo(tmp_path)
    temp_root = tmp_path / "materialize-temp-root"
    temp_root.mkdir()
    monkeypatch.setenv("KEW_WORK_ROOT", str(temp_root))

    # Without an origin an unknown sha is refused before any git transfer
    # (RuntimeError); with one, the origin fetch fails (CalledProcessError).
    with pytest.raises((subprocess.CalledProcessError, RuntimeError)):
        materialize_reviewer_worktree(repo, "deadbeef" * 5, base_ref="main")

    # Flock files stay in place so future claimants use the same inode.
    from kew import work_dirs
    from kew.worktree import claim_checkout, release_checkout_claim

    [lock] = (temp_root / work_dirs.UNSCOPED).iterdir()
    assert lock.suffix == ".lock"
    checkout = lock.with_suffix("")
    assert not checkout.exists()
    claim_checkout(checkout)
    release_checkout_claim(checkout)


def test_advisory_budget_refusal_does_not_hold(tmp_path: Path):
    repo, _base, head_sha = _make_repo(tmp_path)
    now = datetime(2026, 7, 23, tzinfo=UTC)
    with _store(tmp_path) as store:
        store.save(
            RunRecord(
                run_id="prior",
                issue_num=42,
                item_id="42",
                branch="b",
                status="success",
                exit_code=0,
                started_at=now - timedelta(minutes=5),
                estimated_cost_usd=6.0,
                run_kind=ADVERSARIAL_REVIEW_RUN_KIND,
            )
        )
        result = dispatch_reviewer(
            store,
            _config(),
            item_id="42",
            issue_num=42,
            head_sha=head_sha,
            repo_root=repo,
            base_ref="main",
            max_review_usd=5.0,
            run_id="rev-run-4",
            node_epic_ref="epic:demo",
            blocking=False,
            launcher=lambda ctx: CollectedSidecar(
                ctx.log_dir / "s", "d", ParsedVerdict("READY")
            ),
            now=now,
        )
        assert result.outcome == "budget_refused"
        assert store.list_epic_holds("epic:demo") == []


_ASSERT_REMOVED = "def test_widget():\n    assert widget_ok()\n"


def _make_assert_removing_repo(
    tmp_path: Path, head_text: str = _ASSERT_REMOVED
) -> tuple[Path, str]:
    """Base with a two-assert test; head (on a branch) rewrites it to *head_text*,
    by default deleting one assert. Returns (repo, head)."""
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "t")
    test_file = repo / "tests" / "test_widget.py"
    test_file.write_text(
        "def test_widget():\n    assert widget() == 3\n    assert widget_ok()\n"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "checkout", "-q", "-b", "feature")
    test_file.write_text(head_text)
    _git(repo, "commit", "-q", "-am", "head")
    head = run(["git", "rev-parse", "HEAD"], cwd=repo, check=True).stdout.strip()
    return repo, head


def _dispatch_capturing(
    tmp_path: Path, repo: Path, head: str, pr_body: str | None = None
) -> ReviewerLaunchContext:
    return _dispatch_with_result(tmp_path, repo, head, pr_body)[1]


def _dispatch_with_result(
    tmp_path: Path, repo: Path, head: str, pr_body: str | None = None
) -> tuple[ReviewerDispatch, ReviewerLaunchContext]:
    captured: dict[str, ReviewerLaunchContext] = {}

    def launcher(ctx: ReviewerLaunchContext) -> CollectedSidecar:
        captured["ctx"] = ctx
        return CollectedSidecar(
            ctx.log_dir / "s.json", "d", ParsedVerdict(verdict="READY")
        )

    with _store(tmp_path) as store:
        result = dispatch_reviewer(
            store,
            _config(),
            item_id="42",
            issue_num=42,
            head_sha=head,
            repo_root=repo,
            base_ref="main",
            max_review_usd=5.0,
            run_id="rev-integrity",
            node_epic_ref="epic:demo",
            launcher=launcher,
            pr_body=pr_body,
        )
    return result, captured["ctx"]


def test_integrity_report_flags_and_evidence_reach_the_reviewer(tmp_path: Path):
    """#2486: a diff that removes an assert puts the flag, its evidence line and
    the answer requirement into the attested prompt, and hands the flag to the
    parser through the launch context."""
    repo, head = _make_assert_removing_repo(tmp_path)
    result, ctx = _dispatch_with_result(tmp_path, repo, head)

    assert ctx.integrity_flags == ("asserts_removed",)
    assert "## Test-integrity report (host-computed)" in ctx.prompt
    assert "- `asserts_removed` (1 evidence lines)" in ctx.prompt
    assert "tests/test_widget.py:2: assert widget() == 3" in ctx.prompt
    assert "entry for every flag listed" in ctx.prompt
    received = reviewer_received_prompt(ctx.prompt, ctx.head_sha)
    expected_sha = hashlib.sha256(received.encode()).hexdigest()
    assert result.effective_prompt_sha == expected_sha


_EXPECTATION_EDITED = (
    "def test_widget():\n    assert widget() == 4\n    assert widget_ok()\n"
)


def test_an_edited_test_the_pr_body_does_not_name_is_flagged(tmp_path: Path):
    """#2830: an existing test the diff edits, with no Behavior changes line for
    it, is a flag the reviewer must answer before READY, named by the node id
    the merge's held-out gate would report it under."""
    repo, head = _make_assert_removing_repo(tmp_path, _EXPECTATION_EDITED)
    body = "Closes #42\n\n**Behavior changes**\n\nNone.\n"
    ctx = _dispatch_capturing(tmp_path, repo, head, pr_body=body)

    assert "undeclared_test_edits" in ctx.integrity_flags
    assert (
        "tests/test_widget.py:1: tests/test_widget.py::test_widget is edited and "
        "no Behavior changes line names it"
    ) in ctx.prompt
    assert "Behavior changes line `- <node id> — <reason>`" in ctx.prompt


def test_an_edited_test_the_pr_body_names_is_not_flagged(tmp_path: Path):
    """The line the held-out gate accepts is the line that clears the flag."""
    repo, head = _make_assert_removing_repo(tmp_path, _EXPECTATION_EDITED)
    body = (
        "Closes #42\n\n## Behavior changes\n\n"
        "- `tests/test_widget.py::test_widget` — the issue asks for 4 widgets\n"
    )
    ctx = _dispatch_capturing(tmp_path, repo, head, pr_body=body)

    assert "undeclared_test_edits" not in ctx.integrity_flags
    assert "undeclared_test_edits" not in ctx.prompt


def test_an_unread_pr_body_checks_no_test_edit(tmp_path: Path):
    """No body read means nothing to judge the edit against: no flag."""
    repo, head = _make_assert_removing_repo(tmp_path, _EXPECTATION_EDITED)
    ctx = _dispatch_capturing(tmp_path, repo, head, pr_body=None)

    assert "undeclared_test_edits" not in ctx.integrity_flags


def test_integrity_clean_report_requires_no_section(tmp_path: Path):
    """A diff that leaves its tests alone raises no flag and asks for nothing."""
    repo, head = _make_assert_removing_repo(
        tmp_path,
        "def test_widget():\n    assert widget() == 3\n    assert widget_ok()\n"
        "\n\ndef test_more():\n    assert widget_ok()\n",
    )
    ctx = _dispatch_capturing(tmp_path, repo, head)

    assert ctx.integrity_flags == ()
    assert "raised no flags for this diff" in ctx.prompt
    assert "entry for every flag listed" not in ctx.prompt


def test_integrity_flags_not_enforced_under_a_stale_deployed_prompt(tmp_path: Path):
    """A base ref deploying a pre-#2486 prompt shows the flags but enforces none:
    its reviewer was never told the ``test_changes`` contract."""
    repo, head = _make_assert_removing_repo(tmp_path)
    commit_reviewer_artifact(repo, "# an older reviewer prompt\n")
    ctx = _dispatch_capturing(tmp_path, repo, head)

    assert ctx.integrity_flags == ()
    assert "- `asserts_removed`" in ctx.prompt
    assert "entry for every flag listed" not in ctx.prompt


def test_reviewed_head_measures_the_publishing_runs_report(tmp_path: Path):
    """#2494: the reviewer's pinned checkout is where the run that published the
    head gets its test-integrity counts measured into its run report."""
    from kew import dispatch_core
    from kew.config import RunReportsConfig

    repo, head = _make_assert_removing_repo(tmp_path)
    sink = tmp_path / "sink"
    config = DispatchConfig(
        run_reports=RunReportsConfig(enabled=True, sink_path=str(sink))
    )

    def launcher(ctx: ReviewerLaunchContext) -> CollectedSidecar:
        return CollectedSidecar(
            ctx.log_dir / "s.json", "d", ParsedVerdict(verdict="READY")
        )

    with _store(tmp_path) as store:
        store.save(
            RunRecord(
                run_id="author-run",
                issue_num=42,
                item_id="42",
                branch="feature",
                status="success",
                exit_code=0,
                started_at=datetime.now(UTC),
                run_kind="dispatch",
                repo="o/r",
            )
        )
        dispatch_core._record_publication_intent(
            store,
            run_id="author-run",
            item_id="42",
            branch="feature",
            commit_sha=head,
            tree_sha="b" * 40,
            parent_sha="c" * 40,
        )
        dispatch_reviewer(
            store,
            config,
            item_id="42",
            issue_num=42,
            head_sha=head,
            repo_root=repo,
            base_ref="main",
            max_review_usd=5.0,
            run_id="rev-integrity",
            node_epic_ref="epic:demo",
            launcher=launcher,
        )

    report = (sink / "o--r" / "author-run.md").read_text()
    assert "asserts_removed: 1\n" in report
    assert 'integrity_flags: ["asserts_removed"]\n' in report
    assert "widget" not in report


@pytest.mark.parametrize(
    ("returncode", "stdout", "expected"),
    [
        (0, '{"body": "## Behavior changes\\n"}', "## Behavior changes\n"),
        (0, '{"body": null}', ""),
        (0, "{}", None),
        (1, "", None),
        (0, "not json", None),
    ],
)
def test_the_pr_body_read_for_review_is_none_when_unreadable(
    monkeypatch: pytest.MonkeyPatch, returncode: int, stdout: str, expected: str | None
) -> None:
    """The body the review checks edited tests against: an empty body reads as
    "", and anything the API did not answer reads as None (check skipped)."""
    import kew.review_integrity as review_integrity

    seen: list[list[str]] = []

    def fake_run(cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout)

    monkeypatch.setattr(review_integrity, "run", fake_run)

    assert review_integrity.read_pr_body("o/r", 7) == expected
    assert seen == [["gh", "api", "--method", "GET", "repos/o/r/pulls/7"]]


def test_no_pr_reads_no_body(monkeypatch: pytest.MonkeyPatch) -> None:
    import kew.review_integrity as review_integrity

    def fake_run(cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        raise AssertionError(f"no PR, yet ran {cmd}")

    monkeypatch.setattr(review_integrity, "run", fake_run)

    assert review_integrity.read_pr_body("o/r", None) is None
