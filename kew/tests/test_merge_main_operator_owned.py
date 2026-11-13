"""The merge-main step leaves an operator-owned branch alone (#3200).

On 2026-10-25 the step pushed "resolve merge conflicts with main" commits onto
two branches the operator had taken, and one of them reintroduced a symbol the
operator's own fix had removed. A head whose newest commit is the operator's,
or one a live operator merge verify claims, now gets no integration push; the
step records ``operator-owned: integration skipped`` instead.

The branch history is real git: kew's published commit, then the operator's
commit on top. kew's record of what it published is the ``publication_intent``
its host publisher writes. The integration seam stands in for ``kew resolve``,
which in production pushes the merge commit, so a seam that never ran is a
push that never happened.
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from support.resolve_forge import resolve_forge

from kew.audit import Actor, emit
from kew.config import DispatchConfig, LoggingConfig, ReviewConfig
from kew.graph.driver.base_integration import (
    HEAD_MOVED_SKIP,
    INTEGRATION_SKIPPED_EVENT,
    OPERATOR_OWNED_SKIP,
    BaseIntegrationContext,
    BaseIntegrationOutcome,
    BaseIntegrator,
)
from kew.graph.driver.facts import NodeFacts
from kew.graph.driver.overlay import DriverStatus
from kew.graph.driver.review_detach import DetachedReviewSpawner
from kew.graph.driver.review_launcher import sandbox_base_integrator
from kew.graph.driver.review_stage import ReviewStageOutcome, drive_review_stage
from kew.pseudo_epics import REVIEW_EPIC_REF
from kew.publication_attest import attest_published_head
from kew.readiness import (
    Containment,
    RealizedLaunchBoundary,
    compose_realized_profile,
    compose_route_profile,
)
from kew.run_store import RunRecord, SqliteRunStore
from kew.service.verbs import finalize_publication_run
from kew.subprocess_utils import TOOL_PATHS, run

EPIC = "epic:owned"
ITEM = "3148"
BRANCH = f"agent/issue-{ITEM}"
PR = 9
NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _register_git(monkeypatch: pytest.MonkeyPatch) -> None:
    git = shutil.which("git")
    assert git is not None, "git required for the branch fixture"
    monkeypatch.setitem(TOOL_PATHS, "git", git)


@pytest.fixture
def store(tmp_path: Path) -> Any:
    with SqliteRunStore(str(tmp_path / "d.db")) as s:
        yield s


def _git(repo: Path, *args: str) -> str:
    return run(["git", *args], cwd=repo, check=True).stdout.strip()


def _commit(repo: Path, rel: str, text: str, *, author: str) -> str:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    _git(repo, "add", "-A")
    _git(
        repo,
        "-c",
        f"user.name={author}",
        "-c",
        f"user.email={author}@example.invalid",
        "commit",
        "-q",
        "-m",
        f"{author}: {rel}",
    )
    return _git(repo, "rev-parse", "HEAD")


def _branch(tmp_path: Path) -> tuple[str, str]:
    """kew's published commit on the agent branch, then the operator's fix.

    Returns ``(kew_head, operator_head)``.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _commit(
        repo, "frontend/src/routes.ts", "export const atRoute = 1;\n", author="base"
    )
    _git(repo, "checkout", "-q", "-b", BRANCH)
    kew_head = _commit(
        repo,
        "frontend/src/controller.ts",
        "import { atRoute } from './routes';\nexport const c = atRoute;\n",
        author="kew",
    )
    operator_head = _commit(
        repo,
        "frontend/src/routes.ts",
        "export const toRoute = 1;\n",
        author="operator",
    )
    return kew_head, operator_head


def _published(store: SqliteRunStore, commit_sha: str) -> None:
    """kew's host publisher's record that it pushed *commit_sha* on the branch."""
    emit(
        store,
        "publication_intent",
        actor=Actor("policy", "host-publication"),
        run_id="run-kew",
        item_id=ITEM,
        payload={"branch": BRANCH, "commit_sha": commit_sha, "scope": "item"},
    )


def _facts(head: str) -> NodeFacts:
    return NodeFacts(
        issue_num=int(ITEM),
        state="OPEN",
        state_reason=None,
        labels=frozenset(),
        pr_number=PR,
        pr_url=None,
        review_decision=None,
        merge_state_status="DIRTY",
        head_oid=head,
        pr_pushed_at=None,
    )


class _Integrator:
    """The merge-main seam; production's ``kew resolve`` pushes onto the head."""

    def __init__(self) -> None:
        self.pushed_onto: list[str] = []

    def __call__(self, ctx: BaseIntegrationContext) -> BaseIntegrationOutcome:
        self.pushed_onto.append(ctx.head_sha)
        return BaseIntegrationOutcome("integrated")


def _config(tmp_path: Path) -> DispatchConfig:
    return DispatchConfig(
        review=ReviewConfig(
            adversarial=True,
            blocking=True,
            max_rounds=2,
            publish_status=False,
            publish_comment=False,
        ),
        logging=LoggingConfig(dir=str(tmp_path / "logs")),
    )


def _tick(
    store: SqliteRunStore,
    tmp_path: Path,
    head: str,
    integrator: BaseIntegrator,
    *,
    config: DispatchConfig | None = None,
) -> ReviewStageOutcome:
    return drive_review_stage(
        {ITEM: DriverStatus.IN_REVIEW},
        {ITEM: _facts(head)},
        epic_ref=EPIC,
        config=config or _config(tmp_path),
        store=store,
        repo="o/r",
        repo_root=tmp_path,
        base="main",
        reviewer_launcher=None,
        base_integrator=integrator,
        now=NOW,
    )


def _skips(store: SqliteRunStore) -> list[Any]:
    return store.list_audit_events(event=INTEGRATION_SKIPPED_EVENT, item_id=ITEM)


def test_an_operator_commit_on_the_head_gets_no_resolver_push(
    store: SqliteRunStore, tmp_path: Path
) -> None:
    kew_head, operator_head = _branch(tmp_path)
    _published(store, kew_head)
    integrator = _Integrator()

    outcomes = [_tick(store, tmp_path, operator_head, integrator) for _ in range(2)]

    assert integrator.pushed_onto == []
    for outcome in outcomes:
        assert outcome.refused[ITEM].startswith(OPERATOR_OWNED_SKIP)
        assert outcome.held == [] and outcome.integrated == {}
    assert store.list_epic_holds(EPIC) == []
    # Recorded once for the head, however many ticks it waits.
    [skip] = _skips(store)
    assert skip.payload["reason"] == OPERATOR_OWNED_SKIP
    assert skip.payload["head_sha"] == operator_head


def test_a_head_kew_published_itself_is_still_integrated(
    store: SqliteRunStore, tmp_path: Path
) -> None:
    kew_head, _operator_head = _branch(tmp_path)
    _published(store, kew_head)
    integrator = _Integrator()

    outcome = _tick(store, tmp_path, kew_head, integrator)

    assert integrator.pushed_onto == [kew_head]
    assert ITEM in outcome.integrated
    assert _skips(store) == []


def _run_on_branch(store: SqliteRunStore, run_id: str, *, status: str) -> None:
    """A run of this item on the branch, with the launch profile a head
    binding requires of its producer."""
    store.save(
        RunRecord(
            run_id=run_id,
            issue_num=int(ITEM),
            item_id=ITEM,
            branch=BRANCH,
            status=status,  # type: ignore[arg-type]
            exit_code=0,
            started_at=NOW - timedelta(hours=1),
            finished_at=NOW - timedelta(minutes=30),
            error_category=(
                "publication_unavailable" if status == "awaiting_publication" else ""
            ),
        )
    )
    store.record_route_profile(
        compose_realized_profile(
            run_id=run_id,
            attempt_number=1,
            route=compose_route_profile(
                runner="claude-code", requested_model="claude-opus-5"
            ),
            boundary=RealizedLaunchBoundary(
                containment=Containment.NATIVE, auth_path="cli-login"
            ),
        )
    )


def _git_readers(tmp_path: Path, head: str) -> dict[str, Any]:
    """``kew publication attest``'s operator-origin reads, over the real repo.

    The origin's head is the fixture's head; parents and base reachability are
    read from the commits themselves.
    """
    repo = tmp_path / "repo"

    def parents(sha: str) -> list[str]:
        return _git(repo, "rev-list", "--parents", "-n", "1", sha).split()[1:]

    def on_base(sha: str, base: str) -> bool:
        ancestry = run(
            ["git", "merge-base", "--is-ancestor", sha, base], cwd=repo, check=False
        )
        return ancestry.returncode == 0

    return {
        "base_branch": "main",
        "remote_head": lambda _branch: head,
        "commit_parents": parents,
        "commit_on_base": on_base,
    }


def test_a_head_an_operator_attested_through_its_parent_is_theirs(
    store: SqliteRunStore, tmp_path: Path
) -> None:
    """``kew publication attest``: the operator pushed a fix on top of kew's
    published head and bound it through that parent. The head is theirs."""
    kew_head, operator_head = _branch(tmp_path)
    _run_on_branch(store, "run-kew", status="success")
    _published(store, kew_head)
    attest_published_head(
        store,
        branch=BRANCH,
        head_sha=operator_head,
        item_id=ITEM,
        **_git_readers(tmp_path, operator_head),
    )
    integrator = _Integrator()

    outcome = _tick(store, tmp_path, operator_head, integrator)

    assert integrator.pushed_onto == []
    assert outcome.refused[ITEM].startswith(OPERATOR_OWNED_SKIP)
    assert [s.payload["head_sha"] for s in _skips(store)] == [operator_head]


def test_a_head_kew_finalize_bound_is_the_agents_and_is_integrated(
    store: SqliteRunStore, tmp_path: Path
) -> None:
    """``kew finalize``: the operator published the agent's own held output
    by hand and bound it to the run. That head is the agent's, so the step
    still integrates it, although kew published an older head on the branch
    and a human recorded the binding."""
    kew_head, held_head = _branch(tmp_path)
    _run_on_branch(store, "run-kew", status="success")
    _published(store, kew_head)
    _run_on_branch(store, "run-held", status="awaiting_publication")
    config = _config(tmp_path)
    config.project.repo = "o/r"
    finalize_publication_run(
        store, run_id="run-held", config=config, remote_head=lambda _b: held_head
    )
    assert [
        e.payload["commit_sha"]
        for e in store.list_audit_events(event="publication_head_attested")
    ] == [held_head]
    integrator = _Integrator()

    outcome = _tick(store, tmp_path, held_head, integrator)

    assert integrator.pushed_onto == [held_head]
    assert ITEM in outcome.integrated
    assert _skips(store) == []


def test_a_head_with_a_live_operator_claim_gets_no_resolver_push(
    store: SqliteRunStore, tmp_path: Path
) -> None:
    kew_head, _operator_head = _branch(tmp_path)
    _published(store, kew_head)
    store.take_merge_claim(PR, (kew_head,), owner="kew verify tom:1", now=NOW)
    integrator = _Integrator()

    outcome = _tick(store, tmp_path, kew_head, integrator)

    assert integrator.pushed_onto == []
    assert outcome.refused[ITEM].startswith("merge_claimed — kew verify tom:1")
    [skip] = _skips(store)
    assert skip.payload["reason"] == OPERATOR_OWNED_SKIP
    assert skip.payload["head_sha"] == kew_head


class _Child:
    def poll(self) -> None:
        return None


def test_the_detached_stage_spawns_nothing_for_an_operator_owned_head(
    store: SqliteRunStore, tmp_path: Path
) -> None:
    """The production path: the tick decides before reserving or spawning."""
    kew_head, operator_head = _branch(tmp_path)
    _published(store, kew_head)
    config = _config(tmp_path)
    spawned: list[list[str]] = []

    def popen(cmd: list[str], **_kwargs: object) -> _Child:
        spawned.append(cmd)
        return _Child()

    outcome = drive_review_stage(
        {ITEM: DriverStatus.IN_REVIEW},
        {ITEM: _facts(operator_head)},
        epic_ref=EPIC,
        config=config,
        store=store,
        repo="o/r",
        repo_root=tmp_path,
        base="main",
        reviewer_launcher=None,
        now=NOW + timedelta(minutes=1),
        detacher=DetachedReviewSpawner(config, base="main", popen=popen),
        max_concurrent=2,
        worker_cap=4,
    )

    assert spawned == []
    assert store.list_epic_reservations(REVIEW_EPIC_REF) == []
    assert outcome.refused[ITEM].startswith(OPERATOR_OWNED_SKIP)
    assert len(_skips(store)) == 1


def _main_merged_into(tmp_path: Path, head: str) -> tuple[Path, str]:
    """A sibling lands on main, and main merges cleanly into *head*: the tree
    ``kew resolve`` would publish onto it. Returns ``(worktree, base_sha)``."""
    repo = tmp_path / "repo"
    _git(repo, "checkout", "-q", "main")
    base_sha = _commit(
        repo, "frontend/src/panel.ts", "export const p = 1;\n", author="sib"
    )
    _git(repo, "update-ref", "refs/remotes/origin/main", base_sha)
    _git(repo, "checkout", "-q", head)
    _git(
        repo,
        "-c",
        "user.name=kew",
        "-c",
        "user.email=kew@example.invalid",
        "merge",
        "-q",
        "--no-edit",
        "main",
    )
    return repo, base_sha


def test_an_operator_push_between_the_check_and_the_resolve_gets_no_push(
    store: SqliteRunStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tick reads kew's own head and finds it kew's to integrate; the
    operator pushes a fix on top before ``kew resolve`` reads the PR. The
    resolve is pinned to the head the tick checked, so it pushes nothing and
    says the head moved; the node is neither held nor charged a step, and the
    next tick reads the operator's head and leaves it to them."""
    kew_head, operator_head = _branch(tmp_path)
    _run_on_branch(store, "run-kew", status="success")
    _published(store, kew_head)
    worktree, base_sha = _main_merged_into(tmp_path, operator_head)
    config = _config(tmp_path)
    config.project.repo = "o/r"
    config.sandbox.enabled = False
    config.sandbox.dangerously_skip_containment = True
    monkeypatch.setattr("kew.sandbox.detect_runtime_cached", lambda _config: None)
    integrator = sandbox_base_integrator(config, store, epic_ref=EPIC)

    # The forge already holds the operator's push when the resolve reads it.
    with resolve_forge(
        worktree,
        branch=BRANCH,
        pr_head=operator_head,
        item=int(ITEM),
        base_sha=base_sha,
    ) as push:
        checked = _tick(store, tmp_path, kew_head, integrator, config=config)
        reread = _tick(store, tmp_path, operator_head, integrator, config=config)

    assert push.published == []
    assert checked.refused[ITEM].startswith(HEAD_MOVED_SKIP)
    assert f"{kew_head[:12]} -> {operator_head[:12]}" in checked.refused[ITEM]
    assert checked.held == [] and checked.integrated == {}
    assert store.list_epic_holds(EPIC) == []
    assert reread.refused[ITEM].startswith(OPERATOR_OWNED_SKIP)
    assert {(s.payload["reason"], s.payload["head_sha"]) for s in _skips(store)} == {
        (HEAD_MOVED_SKIP, kew_head),
        (OPERATOR_OWNED_SKIP, operator_head),
    }
