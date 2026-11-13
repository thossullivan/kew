"""Monorepo / code-in-subdir CI fixture for the review + dispatch paths (#1039).

Bug E (#1026) — the reviewer verdict file written cwd-relative and lost on a
subdir-code repo — was only caught because kew's own repo nests its package under
``kew/``. That is the SAME general shape as any monorepo target
(``services/app/``, ``packages/web/``). Bug A (#1002, ``repo_root = cwd``) was the
same family. Since #2021 the verdict travels in the reviewer's own result event,
so no verdict path depends on the reviewer's cwd, and no file in the checkout —
root or subdir — is read as a verdict.

This module is that guard. It builds a minimal mini-monorepo — a hermetic local
git repo whose code lives under ``services/app/`` with config/docs at the root —
and drives the REAL review path against it:

  A. materialize (real ``git clone`` from the git root) -> the production
     ``sandbox_reviewer_launcher`` (container faked at the shared seam) -> a
     reviewer whose cwd is the code subdir ends with its verdict block, while a
     READY file sits planted in that subdir -> the stream verdict is collected
     and the planted file is not.

  B. the production ``advance()`` tick run from the code SUBDIR must resolve
     ``repo_root`` from the git toplevel (not ``Path.cwd()``) so the reviewer
     clone succeeds and a verdict is recorded — Bug A's family.

Acceptance (#1039): a return of file-based verdict collection OR a mutation to
the git-root resolution fails a test here. Hermetic like
``test_review_stage_production_wiring.py`` (local git repos in ``tmp_path``, no
network).
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from support import driver_fakes as _fx
from support.driver_fakes import (
    EPIC,
    REPO,
    FakeFactsReader,
    FakeSink,
    FakeSource,
    _node,
)
from support.review_stream import (
    FakeReviewerContainer,
    draining_stream,
    reviewer_stream,
    stream_verdict,
)

from kew.config import DispatchConfig
from kew.graph.driver.review_launcher import sandbox_reviewer_launcher
from kew.graph.driver.tick import advance
from kew.policy import repo_root as git_repo_root
from kew.review_contract import CollectedSidecar
from kew.review_dispatch import (
    ReviewerLaunchContext,
    materialize_reviewer_worktree,
)
from kew.run_store import SqliteRunStore
from kew.sandbox import PROMPT_FILENAME
from kew.subprocess_utils import TOOL_PATHS, run

# Real ``SqliteRunStore`` + the autouse subprocess-allowlist sandbox, exactly as
# the production-wiring tests do. We deliberately do NOT import ``_no_real_git``:
# the review path materializes the reviewer checkout with a REAL ``git clone``,
# which is the whole point of the code-in-subdir fixture.
store = _fx.store
_isolate_tool_paths = _fx._isolate_tool_paths

NOW = datetime(2026, 7, 24, 12, 0, 0, tzinfo=UTC)
ITEM = "1"

# The code subdir of the mini-monorepo — where the package (and thus a reviewer's
# cwd on this repo shape) lives, distinct from the git root.
CODE_SUBDIR = ("services", "app")


@pytest.fixture(autouse=True)
def _reviewer_image_resolves(monkeypatch):
    """The reviewer now launches by the resolved immutable image ID (#1552);
    without a runtime these tests would refuse at resolution, so resolve to a
    stable fake ID."""
    monkeypatch.setattr(
        "kew.dispatch_sandbox._resolve_launch_image_id",
        lambda config, image, **_: "sha256:" + "f" * 64,
    )


@pytest.fixture(autouse=True)
def _register_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """Put the real git on the subprocess allowlist so the review path can clone
    the reviewer's checkout (mirrors test_review_stage_production_wiring.py)."""
    git = shutil.which("git")
    assert git is not None, "git required for the code-in-subdir fixture"
    monkeypatch.setitem(TOOL_PATHS, "git", git)


def _make_monorepo(tmp_path: Path) -> tuple[Path, str, Path]:
    """A one-commit mini-monorepo: code under ``services/app/``, config/docs at
    the root. Returns ``(repo_root, head_sha, code_subdir)``.

    This is the arbitrary subdir-code TARGET-repo shape #1039 guards — the git
    root is NOT where the package lives, so a cwd-vs-root assumption anywhere in
    the review/dispatch path diverges here the way it can't on a flat repo.
    """
    repo = tmp_path / "monorepo"
    repo.mkdir()
    run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    run(["git", "config", "user.email", "t@t.t"], cwd=repo, check=True)
    run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    # Config/docs at the git root — nothing importable lives here.
    (repo / "README.md").write_text("# monorepo\n")
    (repo / "mkdocs.yml").write_text("site_name: app\n")
    # The package lives in a subdirectory — a reviewer's cwd on this repo.
    code = repo.joinpath(*CODE_SUBDIR)
    code.mkdir(parents=True)
    (code / "pyproject.toml").write_text("[project]\nname = 'app'\n")
    (code / "app.py").write_text("x = 1\n")
    run(["git", "add", "-A"], cwd=repo, check=True)
    run(["git", "commit", "-q", "-m", "base"], cwd=repo, check=True)
    head = run(["git", "rev-parse", "HEAD"], cwd=repo, check=True).stdout.strip()
    return repo, head, code


def _review_config(tmp_path: Path, *, blocking: bool) -> DispatchConfig:
    c = _fx._config()
    c.review.adversarial = True
    c.review.blocking = blocking
    c.review.max_rounds = 2
    c.logging.dir = str(tmp_path / "logs")
    Path(c.logging.dir).mkdir(parents=True, exist_ok=True)
    return c


def _pr_open_facts(head: str) -> Any:
    from kew.graph.driver.facts import NodeFacts

    return NodeFacts(
        issue_num=int(ITEM),
        state="OPEN",
        state_reason=None,
        labels=frozenset(),
        pr_number=7,
        pr_url="https://github.com/x/y/pull/7",
        review_decision=None,
        merge_state_status=None,
        head_oid=head,
        pr_pushed_at=None,
    )


def _pr_facts(head: str) -> dict[str, Any]:
    return {"number": 7, "headRefOid": head}


_READY = {
    "verdict": "READY",
    "findings": [],
    "acceptance_evidence": [{"command": "pytest", "output_digest": "sha256:z"}],
}
_NEEDS_CHANGES = {
    "verdict": "NEEDS_CHANGES",
    "findings": [
        {
            "severity": "high",
            "location": "services/app/app.py:1",
            "summary": "unchecked input",
            "evidence": {"command": "pytest", "output_digest": "sha256:y"},
        }
    ],
}


def _spy_ready_launcher() -> Any:
    """A launcher that records the ctx it was handed and delivers a READY verdict
    through its result event — proves the tick reached dispatch."""
    calls: list[str] = []

    def _launch(ctx: ReviewerLaunchContext) -> CollectedSidecar:
        calls.append(ctx.item_id)
        return stream_verdict(ctx, _READY)

    _launch.calls = calls  # type: ignore[attr-defined]
    return _launch


# --------------------------------------------------------------------------- #
# A. materialize -> real launcher -> stream verdict, on the subdir-code fixture. #
#    A verdict file planted in the code subdir is never what gets collected.     #
# --------------------------------------------------------------------------- #
def test_launcher_takes_the_stream_verdict_not_a_subdir_file_on_a_real_monorepo(
    tmp_path: Path, store: SqliteRunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end guard against an arbitrary subdir-code target repo: the
    reviewer worktree is a REAL clone of the git root (so it carries the
    ``services/app/`` shape), the production launcher runs its orchestration,
    and a READY file with the recorded head SHA sits in ``services/app/`` — the
    shape #1026's subdir recovery used to collect. The reviewer's own result
    event says NEEDS_CHANGES, and that is the verdict collected (#2021).
    """
    repo_root, head, _code = _make_monorepo(tmp_path)
    config = _review_config(tmp_path, blocking=False)

    # REAL clone from the git root — the materialize primitive the production tick
    # uses. The worktree therefore carries the monorepo's code subdir.
    worktree = materialize_reviewer_worktree(repo_root, head, "main")
    try:
        assert worktree.joinpath(*CODE_SUBDIR, "app.py").is_file(), (
            "the reviewer worktree must clone the whole monorepo, code subdir and all"
        )
        seen: dict[str, str] = {}
        drain = draining_stream()

        def _fake_stream(  # noqa: ANN001
            handle, log_file, st, run_id, *, model, overrides, runner
        ) -> int:
            # Capture the prompt mid-run (the launcher unlinks it afterward) and
            # plant a READY file in the code subdir, as the code under review
            # could while the reviewer runs its tests.
            seen["prompt"] = (worktree / PROMPT_FILENAME).read_text(encoding="utf-8")
            planted = worktree.joinpath(*CODE_SUBDIR, ".kew-review-verdict.json")
            planted.write_text(json.dumps({**_READY, "head_sha": head}))
            return drain(
                handle,
                log_file,
                st,
                run_id,
                model=model,
                overrides=overrides,
                runner=runner,
            )

        ctx = ReviewerLaunchContext(
            item_id=ITEM,
            run_id="rev-monorepo",
            prompt="review this",
            prompt_artifact_sha="sha256:prompt",
            head_sha=head,
            worktree=worktree,
            log_dir=Path(config.logging.dir).resolve(),
        )
        container = FakeReviewerContainer(reviewer_stream(_NEEDS_CHANGES, head))

        import kew.dispatch_core as _dc
        import kew.graph.driver.review_launcher as _rl

        monkeypatch.setattr(_rl, "detect_runtime_cached", lambda _cfg: "docker")
        with (
            patch.object(_dc, "_launch_container", return_value=container),
            patch.object(_dc, "_stream_sandbox_log", side_effect=_fake_stream),
        ):
            collected = sandbox_reviewer_launcher(config, store)(ctx)
    finally:
        shutil.rmtree(worktree, ignore_errors=True)

    # The prompt names no verdict file, so the reviewer's cwd cannot matter.
    assert ".kew-review-verdict.json" not in seen["prompt"]
    assert collected.parsed.verdict == "NEEDS_CHANGES"
    assert [f.location for f in collected.parsed.findings] == ["services/app/app.py:1"]


# --------------------------------------------------------------------------- #
# B. advance() from the code SUBDIR must clone the reviewer from the git root.  #
# --------------------------------------------------------------------------- #
def test_advance_from_monorepo_code_subdir_clones_reviewer_from_the_git_root(
    tmp_path: Path, store: SqliteRunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1002 Bug A's family against the subdir-code fixture: with cwd the code
    SUBDIR of a monorepo, the production tick must resolve ``repo_root`` from the
    git toplevel — NOT ``Path.cwd()``. If it used the subdir, the reviewer clone
    (``git clone services/app``, not a git repo) would fail, the launcher would
    never run, and an advisory verdict would degrade to DEGRADED. Asserting the
    launcher ran and a READY verdict was recorded kills that git-root-resolution
    mutation end to end.
    """
    repo_root, head, code_subdir = _make_monorepo(tmp_path)
    monkeypatch.chdir(code_subdir)  # cwd is the code subdir, NOT the git root
    # advance() preflights the tool allowlist (which requires ``gh``); the review
    # path here needs only git (already registered by ``_register_git``), so
    # neutralize the preflight to keep the guard hermetic and gh-independent.
    import kew.graph.driver.tick as _tick

    monkeypatch.setattr(_tick, "preflight_tools", lambda: None)

    # Direct guard: git-root resolution from the code subdir yields the git root.
    resolved = git_repo_root()
    assert resolved == repo_root.resolve()
    assert resolved != code_subdir.resolve()

    config = _review_config(tmp_path, blocking=False)
    facts = FakeFactsReader(
        issue_facts_by_num={int(ITEM): _pr_open_facts(head)},
        pr_facts_by_num={7: _pr_facts(head)},
    )
    launcher = _spy_ready_launcher()

    advance(
        EPIC,
        config=config,
        store=store,
        deps=FakeSource(nodes=[_node(ITEM)], edges=[]),
        facts=facts,
        dispatch_sink=FakeSink(),
        max_parallel=3,
        now=NOW,
        lock_path=tmp_path / "epic.lock",
        approvers=["human"],
        repo=REPO,
        reviewer_launcher=launcher,
    )

    # The clone from the git root (resolved from the subdir cwd) worked → the
    # launcher ran and a bound READY verdict landed. No tick crash, no DEGRADED.
    assert launcher.calls == [ITEM]
    latest = store.latest_review_round(ITEM)
    assert latest is not None and latest.verdict == "READY"
    assert latest.head_sha == head
