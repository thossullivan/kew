"""#2149 — the epic driver merges main into a READY branch that stopped merging.

The incident: with siblings landing in parallel, READY PRs fell behind `main`
and conflicted in shared export barrels, fixtures and the factory shell (#2096,
#2102, #2131). The driver had no step for it, so an operator merged `main` in by
hand — and that operator commit then hit #2124, a head bound to no producer,
which the driver refuses to remediate. One manual merge cost a manual
remediation too.

The proof this file carries: a branch conflicting only on an export barrel is
brought current by the driver's own governed step — no operator commit — and the
review/remediation lifecycle keeps running against the new head afterwards. Plus
the failure table: a conflict nothing resolved HOLDs the node with the
conflicting paths named, a step that could not run at all HOLDs rather than
reporting a branch as current, and a head that still merges is reviewed exactly
as before.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

import pytest
from support.driver_fakes import EPIC as FAKE_EPIC
from support.gh_fakes import install_placeholder_forge
from support.review_stream import stream_verdict

from kew.config import DispatchConfig, LoggingConfig, ReviewConfig
from kew.epic_holds import holds_node_out_of_frontier
from kew.graph.domain import NodeStatus
from kew.graph.driver.base_integration import (
    MAX_CONSECUTIVE_STEPS,
    BaseIntegrationContext,
    BaseIntegrationOutcome,
    base_integration_due,
    base_integration_hold_reason,
)
from kew.graph.driver.facts import NodeFacts
from kew.graph.driver.overlay import DriverStatus, ReviewOverlay, overlay_status
from kew.graph.driver.review_stage import RemediationContext, drive_review_stage
from kew.pseudo_epics import REVIEW_EPIC_REF
from kew.review_contract import CollectedSidecar
from kew.review_dispatch import ReviewerLaunchContext
from kew.run_store import SqliteRunStore
from kew.subprocess_utils import run

EPIC = "epic:barrel"
ITEM = "42"
BRANCH = "agent/issue-42"
BARREL = "src/pkg/__init__.py"


@pytest.fixture(autouse=True)
def _placeholder_forge(monkeypatch: pytest.MonkeyPatch) -> None:
    """No ``gh`` call reaches GitHub: the forge answers as ``o/r`` does (#3069)."""
    install_placeholder_forge(monkeypatch)


@pytest.fixture(autouse=True)
def _register_git(monkeypatch):
    import shutil

    from kew.subprocess_utils import TOOL_PATHS

    git = shutil.which("git")
    assert git is not None, "git required for base-integration tests"
    monkeypatch.setitem(TOOL_PATHS, "git", git)


def _git(repo: Path, *args: str) -> str:
    return run(["git", *args], cwd=repo, check=True).stdout.strip()


def _write(repo: Path, rel: str, text: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _diverged_repo(tmp_path: Path) -> tuple[Path, str]:
    """A repo whose PR branch conflicts with ``main`` ONLY on the export barrel.

    Returns ``(repo_root, pr_head_sha)``.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "t")
    _write(repo, BARREL, "from .base import Base\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")

    _git(repo, "checkout", "-q", "-b", BRANCH)
    _write(repo, BARREL, "from .base import Base\nfrom .mine import Mine\n")
    _write(repo, "src/pkg/mine.py", "class Mine: ...\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "feat: mine")
    pr_head = _git(repo, "rev-parse", "HEAD")

    # A sibling lands its own export on main, in the same barrel lines.
    _git(repo, "checkout", "-q", "main")
    _write(repo, BARREL, "from .base import Base\nfrom .sibling import Sibling\n")
    _write(repo, "src/pkg/sibling.py", "class Sibling: ...\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "feat: sibling")
    # Left on `main`, the way an operator checkout sits: the reviewer clone
    # resolves its base ref locally and never reaches for a remote.
    return repo, pr_head


def _facts(head: str, *, merge_state: str | None = "DIRTY") -> NodeFacts:
    return NodeFacts(
        issue_num=int(ITEM),
        state="OPEN",
        state_reason=None,
        labels=frozenset(),
        pr_number=7,
        pr_url=None,
        review_decision=None,
        merge_state_status=merge_state,
        head_oid=head,
        pr_pushed_at=None,
    )


def _config(tmp_path: Path) -> DispatchConfig:
    return DispatchConfig(
        review=ReviewConfig(adversarial=True, blocking=True, max_rounds=2),
        logging=LoggingConfig(dir=str(tmp_path / "logs")),
    )


def _evidence() -> dict[str, str]:
    return {"command": "uv run pytest -q", "output_digest": "sha256:deadbeef"}


def _needs_changes_launcher(ctx: ReviewerLaunchContext) -> CollectedSidecar:
    sidecar = {
        "verdict": "NEEDS_CHANGES",
        "findings": [
            {
                "severity": "high",
                "location": "src/pkg/mine.py:1",
                "summary": "unchecked path",
                "evidence": _evidence(),
            }
        ],
    }
    return stream_verdict(ctx, sidecar)


def _drive(
    store,
    repo: Path,
    facts: NodeFacts,
    statuses: dict[str, DriverStatus],
    *,
    config: DispatchConfig,
    reviewer_launcher=None,
    remediator=None,
    base_integrator=None,
):
    return drive_review_stage(
        statuses,
        {ITEM: facts},
        epic_ref=EPIC,
        config=config,
        store=store,
        repo="o/r",
        repo_root=repo,
        base="main",
        reviewer_launcher=reviewer_launcher,
        remediator=remediator,
        base_integrator=base_integrator,
    )


def test_barrel_conflict_is_integrated_then_the_lifecycle_runs_on_the_new_head(
    tmp_path: Path,
) -> None:
    """The whole point of #2149, end to end on a real conflicting branch.

    Tick 1: the PR no longer merges, so the driver spends the node's one action
    on the governed merge-main step instead of a reviewer — and the step, not an
    operator, lands the merge commit. Tick 2: the moved head is reviewed.
    Tick 3: the NEEDS_CHANGES leg remediates the head the step produced, which
    is the part #2124 broke when a human's merge commit was the new head.
    """
    repo, pr_head = _diverged_repo(tmp_path)
    config = _config(tmp_path)
    statuses = {ITEM: DriverStatus.ADVERSARIAL_REVIEW}
    seen: list[BaseIntegrationContext] = []

    def integrator(ctx: BaseIntegrationContext) -> BaseIntegrationOutcome:
        """Stand in for the governed `kew resolve` step: merge, resolve, land."""
        seen.append(ctx)
        _git(repo, "checkout", "-q", BRANCH)
        run(["git", "merge", "--no-commit", ctx.base], cwd=repo, check=False)
        _write(
            repo,
            BARREL,
            "from .base import Base\nfrom .mine import Mine\n"
            "from .sibling import Sibling\n",
        )
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", f"chore: merge {ctx.base}")
        _git(repo, "checkout", "-q", "main")
        return BaseIntegrationOutcome("integrated")

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        first = _drive(
            store,
            repo,
            _facts(pr_head),
            statuses,
            config=config,
            reviewer_launcher=_needs_changes_launcher,
            base_integrator=integrator,
        )

        # The merge-main step WAS the tick's action: no reviewer, no round.
        assert list(first.integrated) == [ITEM]
        assert first.dispatched == [] and first.held == []
        assert store.latest_review_round(ITEM) is None
        assert statuses[ITEM] is DriverStatus.ADVERSARIAL_REVIEW
        assert [(c.item_id, c.pr_number, c.head_sha, c.base) for c in seen] == [
            (ITEM, 7, pr_head, "main")
        ]

        # The branch is current, carrying BOTH exports, and the driver — not an
        # operator — authored the commit that made it so.
        merged_head = _git(repo, "rev-parse", BRANCH)
        assert merged_head != pr_head
        assert _git(repo, "show", f"{BRANCH}:{BARREL}") == (
            "from .base import Base\nfrom .mine import Mine\n"
            "from .sibling import Sibling"
        )
        assert _git(repo, "merge-base", "--is-ancestor", "main", BRANCH) == ""

        # Tick 2: the moved head is re-reviewed, and the verdict stands on it.
        statuses[ITEM] = DriverStatus.ADVERSARIAL_REVIEW
        second = _drive(
            store,
            repo,
            _facts(merged_head, merge_state="CLEAN"),
            statuses,
            config=config,
            reviewer_launcher=_needs_changes_launcher,
            base_integrator=integrator,
        )
        assert second.dispatched == [ITEM]
        latest = store.latest_review_round(ITEM)
        assert latest is not None
        assert (latest.verdict, latest.head_sha) == ("NEEDS_CHANGES", merged_head)

        # Tick 3: the remediation leg still runs — against the integrated head.
        remediations: list[RemediationContext] = []
        statuses[ITEM] = DriverStatus.REMEDIATING
        third = _drive(
            store,
            repo,
            _facts(merged_head, merge_state="CLEAN"),
            statuses,
            config=config,
            reviewer_launcher=_needs_changes_launcher,
            remediator=lambda ctx: remediations.append(ctx) or None,
            base_integrator=integrator,
        )
        assert third.remediating == [ITEM]
        assert [c.head_sha for c in remediations] == [merged_head]
        assert len(seen) == 1, "a head that merges is never re-integrated"


def test_an_unresolvable_conflict_holds_the_node_naming_the_paths(
    tmp_path: Path,
) -> None:
    repo, pr_head = _diverged_repo(tmp_path)
    config = _config(tmp_path)
    statuses = {ITEM: DriverStatus.ADVERSARIAL_REVIEW}
    launched: list[str] = []

    def integrator(ctx: BaseIntegrationContext) -> BaseIntegrationOutcome:
        return BaseIntegrationOutcome(
            "conflicted",
            detail="kew resolve executed, exit 1",
            conflicted_paths=(BARREL, "tests/conftest.py"),
        )

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        out = _drive(
            store,
            repo,
            _facts(pr_head),
            statuses,
            config=config,
            reviewer_launcher=lambda ctx: launched.append(ctx) or None,
            base_integrator=integrator,
        )

    assert out.held == [ITEM]
    assert statuses[ITEM] is DriverStatus.HELD
    assert launched == [], "a branch that cannot land is not reviewed"
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        [hold] = store.list_epic_holds(EPIC)
    assert hold.reason.startswith("base_integration_conflict: ")
    assert BARREL in hold.reason and "tests/conftest.py" in hold.reason
    assert "main" in hold.reason


@pytest.mark.parametrize(
    ("integrator", "expected"),
    [
        (
            lambda ctx: BaseIntegrationOutcome("failed", detail="push rejected"),
            "push rejected",
        ),
        (
            lambda ctx: (_ for _ in ()).throw(RuntimeError("gh exploded")),
            "RuntimeError: gh exploded",
        ),
    ],
    ids=["reported-failure", "raising-seam"],
)
def test_a_step_that_could_not_run_holds_instead_of_reporting_a_current_branch(
    tmp_path: Path, integrator, expected: str
) -> None:
    repo, pr_head = _diverged_repo(tmp_path)
    statuses = {ITEM: DriverStatus.ADVERSARIAL_REVIEW}

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        out = _drive(
            store,
            repo,
            _facts(pr_head),
            statuses,
            config=_config(tmp_path),
            base_integrator=integrator,
        )
        assert out.held == [ITEM] and out.integrated == {}
        [hold] = store.list_epic_holds(EPIC)

    assert hold.reason.startswith("base_integration_failed: ")
    assert expected in hold.reason
    assert "The branch is unchanged." in hold.reason


def test_no_merge_main_step_wired_is_loud_and_takes_no_review_action(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    repo, pr_head = _diverged_repo(tmp_path)
    statuses = {ITEM: DriverStatus.ADVERSARIAL_REVIEW}
    launched: list[str] = []

    with (
        SqliteRunStore(str(tmp_path / "d.db")) as store,
        caplog.at_level(logging.WARNING),
    ):
        out = _drive(
            store,
            repo,
            _facts(pr_head),
            statuses,
            config=_config(tmp_path),
            reviewer_launcher=lambda ctx: launched.append(ctx) or None,
            base_integrator=None,
        )
        assert store.list_epic_holds(EPIC) == []

    assert out.unwired == [ITEM]
    assert launched == []
    assert "no merge-main step is wired" in caplog.text


@pytest.mark.parametrize("merge_state", ["CLEAN", "BEHIND", "BLOCKED", "UNKNOWN", None])
def test_a_head_that_still_merges_is_reviewed_exactly_as_before(
    tmp_path: Path, merge_state: str | None
) -> None:
    """Only DIRTY means "no longer merges". BEHIND is stale-but-clean, BLOCKED
    and UNSTABLE are check states, and UNKNOWN is GitHub still computing —
    rewriting any of those heads would touch branches with nothing wrong."""
    repo, pr_head = _diverged_repo(tmp_path)
    statuses = {ITEM: DriverStatus.ADVERSARIAL_REVIEW}
    calls: list[BaseIntegrationContext] = []

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        out = _drive(
            store,
            repo,
            _facts(pr_head, merge_state=merge_state),
            statuses,
            config=_config(tmp_path),
            reviewer_launcher=_needs_changes_launcher,
            base_integrator=lambda ctx: (
                calls.append(ctx) or BaseIntegrationOutcome("integrated")
            ),
        )

    assert calls == []
    assert out.dispatched == [ITEM] and out.integrated == {}


@pytest.mark.parametrize(
    ("pr_number", "head"),
    [(None, "a" * 40), (7, None)],
    ids=["no-pr", "no-head"],
)
def test_a_dirty_node_with_nothing_to_publish_onto_is_not_due(
    pr_number: int | None, head: str | None
) -> None:
    facts = NodeFacts(
        issue_num=42,
        state="OPEN",
        state_reason=None,
        labels=frozenset(),
        pr_number=pr_number,
        pr_url=None,
        review_decision=None,
        merge_state_status="DIRTY",
        head_oid=head,
        pr_pushed_at=None,
    )
    assert base_integration_due(facts) is False


def _integration_ctx() -> BaseIntegrationContext:
    return BaseIntegrationContext(
        item_id=ITEM, issue_num=42, pr_number=7, head_sha="a" * 40, base="main"
    )


@pytest.mark.parametrize(
    ("outcome", "conflicts", "expected"),
    [
        (("executed", 0), [], BaseIntegrationOutcome("integrated")),
        (
            ("executed", 1),
            [BARREL, "tests/conftest.py"],
            BaseIntegrationOutcome(
                "conflicted",
                detail="kew resolve executed, exit 1",
                conflicted_paths=(BARREL, "tests/conftest.py"),
            ),
        ),
        (
            ("executed", 1),
            [],
            BaseIntegrationOutcome("failed", detail="kew resolve executed, exit 1"),
        ),
        (
            ("not_run", 2),
            [],
            BaseIntegrationOutcome("failed", detail="kew resolve not_run, exit 2"),
        ),
        (
            (
                "not_run",
                2,
                "profile v1 is refused",
                None,
                "prompt_configuration_refused",
            ),
            [BARREL],
            BaseIntegrationOutcome(
                "failed", detail="prompt_configuration_refused: profile v1 is refused"
            ),
        ),
    ],
    ids=[
        "clean",
        "conflicted",
        "failed-without-conflicts",
        "never-ran",
        "profile-refused",
    ],
)
def test_the_production_step_reads_kew_resolve_exactly(
    tmp_path: Path, outcome, conflicts, expected
) -> None:
    """The adapter over the governed `kew resolve` path (#2149).

    Only exit 0 is an integration. A non-zero exit with observed conflicts
    names them; a non-zero exit without them is a step that could not run, not
    a conflict nobody can point at.
    """
    from kew.command_outcome import CommandOutcome
    from kew.graph.driver.review_launcher import sandbox_base_integrator
    from kew.worktree import ConflictInfo

    def fake_resolve(*, pr_number, config, store, on_conflict, expected_head_sha):
        assert pr_number == 7
        # Pinned to the head the stage read (#3200).
        assert expected_head_sha == "a" * 40
        if conflicts:
            on_conflict(ConflictInfo("merge", list(conflicts), "main", "b" * 40))
        return CommandOutcome(*outcome)

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        integrator = sandbox_base_integrator(
            _config(tmp_path), store, epic_ref=EPIC, resolve=fake_resolve
        )
        assert integrator(_integration_ctx()) == expected


@pytest.mark.parametrize("adversarial", [True, False], ids=["review-on", "review-off"])
def test_the_epic_driver_actually_wires_the_merge_main_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, adversarial: bool
) -> None:
    """ "Built but unwired" is this project's recurring review-stage failure.

    The step only fixes the incident if `kew epic run` hands it to the driver,
    so this asserts the CLI does — and hands None when the governed review
    stage (the thing that re-reviews the integrated delta) is off.
    """
    from unittest.mock import MagicMock

    from kew.cli import epic as epic_cli

    config = _config(tmp_path)
    config.review.adversarial = adversarial
    passed: dict[str, object] = {}
    monkeypatch.setattr(epic_cli._c, "DetachedDispatchSink", MagicMock())
    monkeypatch.setattr(
        epic_cli._c, "advance", lambda *a, **k: passed.update(k) or MagicMock()
    )

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        epic_cli._advance_epic(
            EPIC,
            config=config,
            store=store,
            deps=MagicMock(),
            facts=MagicMock(),
            repo="o/r",
            max_parallel=1,
            approvers=[],
            interval=30,
        )

    integrator = passed["base_integrator"]
    assert (integrator is not None) is adversarial
    if adversarial:
        assert callable(integrator)
        assert hasattr(integrator, "bind_store"), "must rebind per worker handle"


# --------------------------------------------------------------------------- #
# Regressions on the first cut of #2149 — the step reached nothing it named.    #
# --------------------------------------------------------------------------- #
def _ready_overlay(head: str) -> ReviewOverlay:
    """The overlay of a node whose READY verdict stands on the current head."""
    return ReviewOverlay(
        enabled=True,
        round_count=1,
        latest_verdict="READY",
        latest_head_sha=head,
        current_head_sha=head,
    )


def test_a_ready_branch_is_the_status_the_step_has_to_reach(tmp_path: Path) -> None:
    """The population #2149 names — a READY branch — reads IN_REVIEW, not
    ADVERSARIAL_REVIEW.

    A READY verdict bound to the current head is the one review state that
    falls THROUGH the review lifecycle (``overlay._persisted_verdict_status``
    returns None for it) to IN_REVIEW. The first cut drove the merge-main step
    off ADVERSARIAL_REVIEW/REMEDIATING alone, so the branch the CLI comment,
    the module docstring and the docs all point at was the one branch it could
    never touch: it fell straight to the merge pass's ``ci_pending`` hold,
    exactly as before the step existed.
    """
    repo, pr_head = _diverged_repo(tmp_path)
    facts = _facts(pr_head)
    assert (
        overlay_status(
            facts, [], reserved=False, held_reason=None, review=_ready_overlay(pr_head)
        )
        is DriverStatus.IN_REVIEW
    )

    statuses = {ITEM: DriverStatus.IN_REVIEW}
    seen: list[BaseIntegrationContext] = []
    launched: list[object] = []

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        out = _drive(
            store,
            repo,
            facts,
            statuses,
            config=_config(tmp_path),
            reviewer_launcher=lambda ctx: launched.append(ctx) or None,
            remediator=lambda ctx: launched.append(ctx) or None,
            base_integrator=lambda ctx: (
                seen.append(ctx) or BaseIntegrationOutcome("integrated")
            ),
        )
        assert store.list_epic_holds(EPIC) == []

    assert [c.head_sha for c in seen] == [pr_head]
    assert list(out.integrated) == [ITEM]
    assert launched == [], "a standing READY verdict is never re-reviewed"
    assert out.dispatched == [] and out.held == []
    assert statuses[ITEM] is DriverStatus.IN_REVIEW


@pytest.mark.parametrize("merge_state", ["CLEAN", "BEHIND", "UNKNOWN", None])
def test_an_in_review_node_that_still_merges_is_left_entirely_alone(
    tmp_path: Path, merge_state: str | None
) -> None:
    """Reaching IN_REVIEW must not put READY nodes back into the review loop.

    The merge-main step is the ONLY action an IN_REVIEW node is ever due: its
    verdict already stands on the current head, so a reviewer or a remediation
    dispatched here would spend a round (and a budget) re-litigating it.
    """
    repo, pr_head = _diverged_repo(tmp_path)
    statuses = {ITEM: DriverStatus.IN_REVIEW}
    acted: list[object] = []

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        out = _drive(
            store,
            repo,
            _facts(pr_head, merge_state=merge_state),
            statuses,
            config=_config(tmp_path),
            reviewer_launcher=lambda ctx: acted.append(ctx) or None,
            remediator=lambda ctx: acted.append(ctx) or None,
            base_integrator=lambda ctx: (
                acted.append(ctx) or BaseIntegrationOutcome("integrated")
            ),
        )
        assert store.list_epic_holds(EPIC) == []

    assert acted == []
    assert (out.dispatched, out.remediating, list(out.integrated)) == ([], [], [])
    assert out.held == [] and out.unwired == []
    assert statuses[ITEM] is DriverStatus.IN_REVIEW


@pytest.mark.parametrize(
    "outcome",
    [
        BaseIntegrationOutcome(
            "conflicted",
            detail="kew resolve executed, exit 1",
            conflicted_paths=(BARREL,),
        ),
        BaseIntegrationOutcome("failed", detail="push rejected"),
    ],
    ids=["conflicted", "failed"],
)
def test_the_holds_are_sticky_because_the_overlay_honors_them(
    tmp_path: Path, outcome: BaseIntegrationOutcome
) -> None:
    """Both hold reasons route the node to HELD — the only thing making the
    step "not re-tried every tick".

    The first cut wrote the reasons in ``base_integration`` alone. Unmatched by
    ``holds_node_out_of_frontier``, the overlay left the node reading
    ADVERSARIAL_REVIEW, the stage found it due again, and — the seam has no
    in-flight guard of its own — it spent another governed ``kew resolve`` run
    on the same unresolvable conflict on every single tick.
    """
    repo, pr_head = _diverged_repo(tmp_path)
    reason = base_integration_hold_reason(outcome, base="main")
    assert holds_node_out_of_frontier(reason)

    facts = _facts(pr_head)
    statuses = {ITEM: DriverStatus.ADVERSARIAL_REVIEW}
    calls: list[BaseIntegrationContext] = []

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        first = _drive(
            store,
            repo,
            facts,
            statuses,
            config=_config(tmp_path),
            base_integrator=lambda ctx: calls.append(ctx) or outcome,
        )
        assert first.held == [ITEM]
        [hold] = store.list_epic_holds(EPIC)
        assert hold.reason == reason

        # The next tick's overlay reads the hold the stage just wrote...
        assert (
            overlay_status(
                facts,
                [],
                reserved=False,
                held_reason=hold.reason,
                review=ReviewOverlay(
                    enabled=True,
                    round_count=0,
                    current_head_sha=pr_head,
                    observed_status=NodeStatus.IN_REVIEW,
                ),
            )
            is DriverStatus.HELD
        )
        # ...and a HELD node is not due, so no second `kew resolve` is spent.
        statuses[ITEM] = DriverStatus.HELD
        second = _drive(
            store,
            repo,
            facts,
            statuses,
            config=_config(tmp_path),
            base_integrator=lambda ctx: calls.append(ctx) or outcome,
        )
        assert second.held == [] and list(second.integrated) == []

    assert len(calls) == 1, "an unresolvable conflict is not re-resolved every tick"


@pytest.mark.parametrize("standing_ready", [True, False], ids=["ready", "in-review"])
def test_the_tick_reports_the_merge_main_step_to_the_operator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, standing_ready: bool
) -> None:
    """The step is reported, through the real ``advance()`` tick (#2149).

    A node whose whole tick went to the merge-main step is neither held nor
    dispatched and keeps its status, so nothing else in the tick's report shows
    it: the first cut collected ``ReviewStageOutcome.integrated`` and then
    dropped it, leaving the operator a line that reads like an untouched
    in-review node while the driver rewrote its branch.

    Both populations the step serves, because they fail differently: a standing
    READY verdict (IN_REVIEW) is the node the step could not even reach, and a
    node still in review (ADVERSARIAL_REVIEW) is the one it reached and then
    said nothing about.
    """
    from support.driver_fakes import EPIC as FAKE_EPIC
    from support.driver_fakes import REPO, FakeFactsReader, FakeSink, FakeSource, _node

    from kew.graph.driver.result import TerminalSignal
    from kew.graph.driver.tick import advance

    repo, pr_head = _diverged_repo(tmp_path)
    monkeypatch.chdir(repo)
    config = _config(tmp_path)
    facts = _facts(pr_head)
    seen: list[BaseIntegrationContext] = []

    with SqliteRunStore(str(tmp_path / "d.db"), project_root=tmp_path) as store:
        if standing_ready:
            store.record_review_round(
                REVIEW_EPIC_REF, ITEM, head_sha=pr_head, verdict="READY"
            )
        result = advance(
            FAKE_EPIC,
            config=config,
            store=store,
            deps=FakeSource(nodes=[_node(ITEM)], edges=[]),
            facts=FakeFactsReader(
                issue_facts_by_num={int(ITEM): facts},
                pr_facts_by_num={
                    7: {
                        "number": 7,
                        "state": "OPEN",
                        "headRefOid": pr_head,
                        "mergeStateStatus": "DIRTY",
                    }
                },
            ),
            dispatch_sink=FakeSink(),
            max_parallel=3,
            now=datetime(2026, 9, 28, 12, 0, tzinfo=UTC),
            lock_path=tmp_path / "epic.lock",
            approvers=["human"],
            repo=REPO,
            reviewer_launcher=_needs_changes_launcher,
            base_integrator=lambda ctx: (
                seen.append(ctx) or BaseIntegrationOutcome("integrated")
            ),
        )

    assert [c.head_sha for c in seen] == [pr_head], "the tick really ran the step"
    waiting = {w.node_id: w.reason for w in result.waiting}
    assert "merge-main step" in waiting.get(ITEM, "")
    assert "main" in waiting.get(ITEM, "")
    # Integrating a base is review-stage progress, never a stalled epic.
    assert result.signal is TerminalSignal.PROGRESSING


# --------------------------------------------------------------------------- #
# Regressions on round 2 of #2149 — the SAME tick's merge pass undid the step. #
# --------------------------------------------------------------------------- #
#
# The review stage runs BEFORE the merge pass, on facts both of them share. So
# the moment the merge-main step acts, the tick's facts for that node describe a
# branch that no longer exists — and the merge pass, reading `DIRTY` off them,
# wrote its own sticky `ci_pending` hold on top of the step's work. That single
# write broke the step both ways, which is why these are tick-level tests: at
# `drive_review_stage` alone both ticks look perfect.

#: The approving human. Must be in ``approvers`` AND resolve to a write
#: permission, or the merge pass skips at ``awaiting-approval`` and never
#: reaches the hold-writing branch these regressions are about.
REVIEWER = "reviewer-human"


def _approved_pr(head: str, *, merge_state: str) -> dict[str, object]:
    """An open PR at *head* carrying a verified human approval (#4)."""
    return {
        "number": 7,
        "state": "OPEN",
        "reviewDecision": "APPROVED",
        "mergeStateStatus": merge_state,
        "headRefOid": head,
        "headRefName": BRANCH,
        "commits": [],
        "latestReviews": [
            {
                "state": "APPROVED",
                # Postdates the recorded verdict, so N1 reads a fresh approval
                # and the governed-review gate passes the candidate through to
                # the CI/mergeability branch.
                "submittedAt": "2099-01-01T00:00:00Z",
                "author": {
                    "login": REVIEWER,
                    "is_bot": False,
                    "__typename": "User",
                },
            }
        ],
        "author": {"login": "kew-agent"},
    }


def _advance_tick(
    store,
    *,
    config: DispatchConfig,
    head: str,
    merge_state: str,
    base_integrator,
    lock_path: Path,
    now: datetime,
    reviewer_launcher=None,
):
    """One real ``advance()`` tick over a single approved, review-enabled node."""
    from support.driver_fakes import REPO, FakeFactsReader, FakeSink, FakeSource, _node

    from kew.graph.driver.tick import advance

    return advance(
        FAKE_EPIC,
        config=config,
        store=store,
        deps=FakeSource(nodes=[_node(ITEM)], edges=[]),
        facts=FakeFactsReader(
            issue_facts_by_num={int(ITEM): _facts(head, merge_state=merge_state)},
            pr_facts_by_num={7: _approved_pr(head, merge_state=merge_state)},
            permissions_by_login={REVIEWER: "write"},
        ),
        dispatch_sink=FakeSink(),
        max_parallel=3,
        now=now,
        lock_path=lock_path,
        approvers=[REVIEWER],
        repo=REPO,
        reviewer_launcher=reviewer_launcher,
        base_integrator=base_integrator,
    )


def _integrating_merge(repo: Path):
    """The governed step, really landing ``main`` on the branch."""

    def integrator(ctx: BaseIntegrationContext) -> BaseIntegrationOutcome:
        _git(repo, "checkout", "-q", BRANCH)
        run(["git", "merge", "--no-commit", ctx.base], cwd=repo, check=False)
        _write(
            repo,
            BARREL,
            "from .base import Base\nfrom .mine import Mine\n"
            "from .sibling import Sibling\n",
        )
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", f"chore: merge {ctx.base}")
        _git(repo, "checkout", "-q", "main")
        return BaseIntegrationOutcome("integrated")

    return integrator


def test_the_integrated_node_is_not_re_held_by_the_same_ticks_merge_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The step's own tick must not wedge the node it just unblocked.

    The merge pass runs after the review stage on the SAME facts, so for a
    standing-READY (IN_REVIEW) node it read the `DIRTY` state the step had just
    resolved and held the node `ci_pending`. That hold is sticky, HELD is a
    status `_due_nodes` cannot reach, and the merge gate refuses the verdict the
    moved head just staled — so the node the step exists for was never
    re-reviewed and never merged, the exact opposite of the documented "the new
    head is reviewed on the next tick". Two ticks, because one tick cannot tell
    a missing hold from a harmless one.
    """
    repo, pr_head = _diverged_repo(tmp_path)
    monkeypatch.chdir(repo)
    config = _config(tmp_path)
    integrator = _integrating_merge(repo)

    with SqliteRunStore(str(tmp_path / "d.db"), project_root=tmp_path) as store:
        store.record_review_round(
            REVIEW_EPIC_REF, ITEM, head_sha=pr_head, verdict="READY"
        )
        first = _advance_tick(
            store,
            config=config,
            head=pr_head,
            merge_state="DIRTY",
            base_integrator=integrator,
            lock_path=tmp_path / "epic.lock",
            now=datetime(2026, 9, 28, 12, 0, tzinfo=UTC),
        )

        assert store.list_epic_holds(FAKE_EPIC) == [], (
            "the merge pass held the node on the facts the step had just spent"
        )
        assert first.held == () and first.merged == ()
        assert "merge-main step" in {w.node_id: w.reason for w in first.waiting}[ITEM]

        merged_head = _git(repo, "rev-parse", BRANCH)
        assert merged_head != pr_head

        # Tick 2 — the claim the docs make. The moved head stales the READY
        # verdict, so the node is due a reviewer round again and gets one.
        second = _advance_tick(
            store,
            config=config,
            head=merged_head,
            merge_state="CLEAN",
            base_integrator=integrator,
            lock_path=tmp_path / "epic.lock",
            reviewer_launcher=_needs_changes_launcher,
            now=datetime(2026, 9, 28, 12, 5, tzinfo=UTC),
        )
        latest = store.latest_review_round(ITEM)

    # The round the node was wedged out of: a fresh reviewer verdict, recorded
    # against the head the step produced.
    assert latest is not None
    assert (latest.round, latest.verdict, latest.head_sha) == (
        2,
        "NEEDS_CHANGES",
        merged_head,
    )
    assert {w.node_id: w.reason for w in second.waiting}[ITEM].startswith(
        "adversarial_review"
    )
    assert second.held == () and second.stuck == ()


def test_a_conflict_hold_is_never_downgraded_to_ci_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hold the step writes has to survive every later merge pass.

    ``hold_epic_node`` upserts the reason, so the merge pass rewrote
    ``base_integration_conflict`` to ``ci_pending`` — losing the conflicting
    paths that are the one fact an operator can act on, and replacing an
    unhold-only hold with one the docs describe as clearing itself. It happened
    in the step's own tick (pre-step facts) and again on every tick after it,
    where the node is HELD and still a merge candidate.
    """
    repo, pr_head = _diverged_repo(tmp_path)
    monkeypatch.chdir(repo)
    config = _config(tmp_path)
    conflict = BaseIntegrationOutcome(
        "conflicted", detail="kew resolve executed, exit 1", conflicted_paths=(BARREL,)
    )
    expected = base_integration_hold_reason(conflict, base="main")
    calls: list[BaseIntegrationContext] = []

    with SqliteRunStore(str(tmp_path / "d.db"), project_root=tmp_path) as store:
        store.record_review_round(
            REVIEW_EPIC_REF, ITEM, head_sha=pr_head, verdict="READY"
        )
        first = _advance_tick(
            store,
            config=config,
            head=pr_head,
            merge_state="DIRTY",
            base_integrator=lambda ctx: calls.append(ctx) or conflict,
            lock_path=tmp_path / "epic.lock",
            now=datetime(2026, 9, 28, 12, 0, tzinfo=UTC),
        )
        assert first.held == (ITEM,)
        assert [h.reason for h in store.list_epic_holds(FAKE_EPIC)] == [expected]
        assert BARREL in expected

        # Tick 2: the node is HELD now, which is still a merge candidate — and
        # the branch is still DIRTY, so the pass reaches the same write again.
        second = _advance_tick(
            store,
            config=config,
            head=pr_head,
            merge_state="DIRTY",
            base_integrator=lambda ctx: calls.append(ctx) or conflict,
            lock_path=tmp_path / "epic.lock",
            now=datetime(2026, 9, 28, 12, 5, tzinfo=UTC),
        )
        assert second.held == (ITEM,)
        assert [h.reason for h in store.list_epic_holds(FAKE_EPIC)] == [expected]

    assert len(calls) == 1, "an unresolvable conflict is not re-resolved every tick"


# --------------------------------------------------------------------------- #
# Regressions on round 3 of #2149 — reachability and the bound, one coin.      #
# --------------------------------------------------------------------------- #
#
# Skipping the merge pass for the node the step just acted on (round 2) took
# away the `ci_pending` hold that used to stop a repeating step by accident, so
# the step needs a bound of its own. And the same `ci_pending` hold, written one
# cause earlier, kept the step from ever reaching a node that needed it.


def _held(store, epic_ref: str = EPIC) -> list[str]:
    return [h.reason for h in store.list_epic_holds(epic_ref)]


def test_a_step_that_keeps_reporting_success_is_bounded_and_then_holds(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A merge-main step that reports ``integrated`` and changes nothing.

    ``integrated`` is the one outcome that holds nothing and leaves the node due
    again, and round 2 stopped the merge pass from classifying that node — so
    nothing bounded it: the driver spent a governed ``kew resolve`` run on it
    every tick forever, reported the tick as PROGRESSING, and left no hold row
    for the stall clock to run against. The bound ends it with the one thing an
    operator can act on: a hold that says the step kept claiming success.
    """
    repo, pr_head = _diverged_repo(tmp_path)
    config = _config(tmp_path)
    facts = _facts(pr_head)
    calls: list[BaseIntegrationContext] = []

    def liar(ctx: BaseIntegrationContext) -> BaseIntegrationOutcome:
        calls.append(ctx)
        return BaseIntegrationOutcome("integrated")

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        for _ in range(MAX_CONSECUTIVE_STEPS):
            statuses = {ITEM: DriverStatus.IN_REVIEW}
            out = _drive(
                store, repo, facts, statuses, config=config, base_integrator=liar
            )
            assert list(out.integrated) == [ITEM] and out.held == []
            assert _held(store) == []

        with caplog.at_level(logging.ERROR):
            statuses = {ITEM: DriverStatus.IN_REVIEW}
            spent = _drive(
                store, repo, facts, statuses, config=config, base_integrator=liar
            )

        assert len(calls) == MAX_CONSECUTIVE_STEPS, (
            "the bound has to stop the run BEFORE the seam, not report it after"
        )
        assert spent.held == [ITEM] and spent.integrated == {}
        assert statuses[ITEM] is DriverStatus.HELD
        [reason] = _held(store)

        # The hold is honored, so the node is out of the frontier and the stall
        # clock runs against a row — the two things the loop had neither of.
        assert holds_node_out_of_frontier(reason)
        assert reason.startswith("base_integration_failed: ")
        assert f"ran {MAX_CONSECUTIVE_STEPS} times in a row" in reason
        assert "does not merge into main" in reason
        assert "reported success each time" in caplog.text

        # `kew epic unhold` is the documented recovery, so it has to BE one:
        # the released node gets a fresh allowance instead of being re-held on
        # the next tick having tried nothing.
        store.clear_epic_hold(ITEM)
        statuses = {ITEM: DriverStatus.IN_REVIEW}
        after = _drive(
            store, repo, facts, statuses, config=config, base_integrator=liar
        )
        assert list(after.integrated) == [ITEM]
        assert len(calls) == MAX_CONSECUTIVE_STEPS + 1


def _review_run(store, item_id: str, *, ordinal: int) -> None:
    """The run row a reviewer round leaves behind for the node.

    The production reviewer launcher writes one per round — it is the same
    ledger ``_consecutive_refusals`` bounds the sandbox-refusal retry with. The
    launcher seam these tests drive is a bare callable, so the row is written
    here instead of being faked into existence by a fake.
    """
    from kew.run_store import ADVERSARIAL_REVIEW_RUN_KIND, RunRecord

    now = datetime.now(UTC)
    store.save(
        RunRecord(
            run_id=f"review-{ordinal}",
            issue_num=int(item_id),
            item_id=item_id,
            branch=BRANCH,
            status="success",
            exit_code=0,
            started_at=now,
            finished_at=now,
            run_kind=ADVERSARIAL_REVIEW_RUN_KIND,
        )
    )


def test_the_bound_counts_a_streak_not_a_lifetime(tmp_path: Path) -> None:
    """A long-lived branch integrates its base as often as the base moves.

    The bound is only ever allowed to catch a step that changes nothing, and a
    step that worked is always followed by other work for the node: the moved
    head stales the verdict, and the next tick reviews it. That review run ends
    the streak, so integrations spread across a node's life never add up to the
    cap between them.
    """
    repo, pr_head = _diverged_repo(tmp_path)
    config = _config(tmp_path)
    calls: list[BaseIntegrationContext] = []
    rounds = MAX_CONSECUTIVE_STEPS + 2

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        for ordinal in range(rounds):
            statuses = {ITEM: DriverStatus.IN_REVIEW}
            out = _drive(
                store,
                repo,
                _facts(pr_head),
                statuses,
                config=config,
                base_integrator=lambda ctx: (
                    calls.append(ctx) or BaseIntegrationOutcome("integrated")
                ),
            )
            assert list(out.integrated) == [ITEM], f"held on round {ordinal}"
            assert _held(store) == []
            # The head moved, so the next tick's reviewer round runs.
            _review_run(store, ITEM, ordinal=ordinal)

    assert len(calls) == rounds


def _barrel(*, mine: bool, extra: int) -> str:
    """The export barrel every sibling lands in — the file #2096 conflicted on."""
    lines = ["from .base import Base\n"]
    if mine:
        lines.append("from .mine import Mine\n")
    lines.append("from .sibling import Sibling\n")
    lines += [f"from .sibling{i} import Sibling{i}\n" for i in range(extra)]
    return "".join(lines)


def _land_sibling(repo: Path, ordinal: int) -> None:
    """Another sibling lands its export on ``main``, in the same barrel lines."""
    _git(repo, "checkout", "-q", "main")
    _write(repo, BARREL, _barrel(mine=False, extra=ordinal + 1))
    _write(repo, f"src/pkg/sibling{ordinal}.py", f"class Sibling{ordinal}: ...\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", f"feat: sibling {ordinal}")


def test_a_step_that_keeps_moving_the_head_is_never_bounded(tmp_path: Path) -> None:
    """A busy epic: the base keeps moving and every step really integrates it.

    The bound is only ever allowed to catch a step that changes nothing, and
    the streak used to count marker rows alone — with the review run in between
    as its only way of ending. But the merge-main step OUTRANKS the review
    action (``_drive_one_node``), so a node that is DIRTY on every tick never
    gets a run of any other kind in between: three honest integrations in a row,
    each moving the head onto a base that had moved again, HELD the node under a
    reason that said the step "reported success each time" while the branch
    stood still. It had moved three times. The streak therefore counts only the
    steps written at the head this tick is acting on.
    """
    repo, pr_head = _diverged_repo(tmp_path)
    config = _config(tmp_path)
    seen: list[BaseIntegrationContext] = []
    landed = 0
    ticks = MAX_CONSECUTIVE_STEPS + 2

    def integrator(ctx: BaseIntegrationContext) -> BaseIntegrationOutcome:
        """Honest: it really merges the base in and really moves the head."""
        seen.append(ctx)
        _git(repo, "checkout", "-q", BRANCH)
        run(["git", "merge", "--no-commit", ctx.base], cwd=repo, check=False)
        _write(repo, BARREL, _barrel(mine=True, extra=landed))
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", f"chore: merge {ctx.base}")
        _git(repo, "checkout", "-q", "main")
        return BaseIntegrationOutcome("integrated")

    head = pr_head
    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        for tick in range(ticks):
            statuses = {ITEM: DriverStatus.ADVERSARIAL_REVIEW}
            out = _drive(
                store,
                repo,
                _facts(head),
                statuses,
                config=config,
                reviewer_launcher=_needs_changes_launcher,
                base_integrator=integrator,
            )
            assert list(out.integrated) == [ITEM], f"held on tick {tick}"
            assert out.held == [] and _held(store) == []
            assert out.dispatched == [], "the step is the tick's one action"

            moved = _git(repo, "rev-parse", BRANCH)
            assert moved != head, f"the step did not move the head on tick {tick}"
            assert _git(repo, "merge-base", "--is-ancestor", "main", BRANCH) == ""
            head = moved
            # The base moves again before the next tick: the node reads DIRTY
            # once more, so the step — not a reviewer — is what it is due, and
            # nothing else is ever written between two markers.
            _land_sibling(repo, landed)
            landed += 1

        assert len(seen) == ticks
        assert store.latest_review_round(ITEM) is None

    # Every head the step read is distinct — the streak has nothing to count.
    assert len({ctx.head_sha for ctx in seen}) == ticks


def test_a_node_wedged_on_ci_pending_still_gets_the_merge_main_step(
    tmp_path: Path,
) -> None:
    """The wedge one cause further back than the one round 2 fixed.

    ``ci_pending`` is written for red CI and for any non-green merge state, and
    it routes the node to HELD. A node already carrying it when its branch goes
    DIRTY therefore read HELD — a status the due pass did not admit — while the
    merge pass's only answer to DIRTY was to write that same hold again. The
    node never got the step, which is the exact failure #2149 exists to remove
    and the exact opposite of what the docs promise for a DIRTY branch.
    """
    repo, pr_head = _diverged_repo(tmp_path)
    statuses = {ITEM: DriverStatus.HELD}
    seen: list[BaseIntegrationContext] = []

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        store.hold_epic_node(EPIC, ITEM, "ci_pending")
        out = _drive(
            store,
            repo,
            _facts(pr_head),
            statuses,
            config=_config(tmp_path),
            reviewer_launcher=_needs_changes_launcher,
            base_integrator=lambda ctx: (
                seen.append(ctx) or BaseIntegrationOutcome("integrated")
            ),
        )
        # The hold was derived from the facts the step has just spent. Left in
        # place it would put the node straight back where it was.
        assert _held(store) == []

    assert [c.head_sha for c in seen] == [pr_head]
    assert list(out.integrated) == [ITEM]
    assert out.dispatched == [] and out.held == []
    assert statuses[ITEM] is DriverStatus.IN_REVIEW


@pytest.mark.parametrize(
    "reason",
    [
        "review_rounds_exhausted",
        "budget_preflight: $12.00 over the $10.00 cap",
        "base_integration_conflict: main no longer merges into this PR branch",
        "base_integration_failed: the merge-main step ran 3 times in a row",
    ],
)
def test_a_node_parked_under_someone_elses_hold_is_left_alone(
    tmp_path: Path, reason: str
) -> None:
    """Only ``ci_pending`` re-opens a HELD node to the step.

    Every other honored hold belongs to a different writer and clears on a
    ``kew epic unhold``: running a governed ``kew resolve`` against a node an
    operator — or the step's own bound — parked is precisely the retry those
    holds exist to stop.
    """
    repo, pr_head = _diverged_repo(tmp_path)
    statuses = {ITEM: DriverStatus.HELD}
    acted: list[object] = []

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        store.hold_epic_node(EPIC, ITEM, reason)
        out = _drive(
            store,
            repo,
            _facts(pr_head),
            statuses,
            config=_config(tmp_path),
            reviewer_launcher=lambda ctx: acted.append(ctx) or None,
            base_integrator=lambda ctx: (
                acted.append(ctx) or BaseIntegrationOutcome("integrated")
            ),
        )
        assert _held(store) == [reason]

    assert acted == []
    assert out.integrated == {} and out.held == [] and out.dispatched == []
    assert statuses[ITEM] is DriverStatus.HELD


def test_the_tick_unwedges_a_ci_pending_node_and_reviews_the_moved_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wedge, through the real tick, and the recovery the docs promise.

    Two ticks: the first has to take a node the merge pass had already parked on
    ``ci_pending`` and run the step anyway — without the same tick's merge pass
    writing the hold straight back — and the second has to review the head the
    step produced.
    """
    repo, pr_head = _diverged_repo(tmp_path)
    monkeypatch.chdir(repo)
    config = _config(tmp_path)
    integrator = _integrating_merge(repo)

    with SqliteRunStore(str(tmp_path / "d.db"), project_root=tmp_path) as store:
        store.record_review_round(
            REVIEW_EPIC_REF, ITEM, head_sha=pr_head, verdict="READY"
        )
        # An earlier pass parked the node: red CI, then the base moved under it.
        store.hold_epic_node(FAKE_EPIC, ITEM, "ci_pending")
        first = _advance_tick(
            store,
            config=config,
            head=pr_head,
            merge_state="DIRTY",
            base_integrator=integrator,
            lock_path=tmp_path / "epic.lock",
            now=datetime(2026, 9, 28, 12, 0, tzinfo=UTC),
        )

        assert _held(store, FAKE_EPIC) == [], "the node stayed wedged on ci_pending"
        assert first.held == () and first.stuck == ()
        assert "merge-main step" in {w.node_id: w.reason for w in first.waiting}[ITEM]

        merged_head = _git(repo, "rev-parse", BRANCH)
        assert merged_head != pr_head

        second = _advance_tick(
            store,
            config=config,
            head=merged_head,
            merge_state="CLEAN",
            base_integrator=integrator,
            lock_path=tmp_path / "epic.lock",
            reviewer_launcher=_needs_changes_launcher,
            now=datetime(2026, 9, 28, 12, 5, tzinfo=UTC),
        )
        latest = store.latest_review_round(ITEM)

    assert latest is not None
    assert (latest.verdict, latest.head_sha) == ("NEEDS_CHANGES", merged_head)
    assert second.held == () and second.stuck == ()


def test_a_refused_resolve_profile_holds_with_its_reason_and_exit_parity(
    tmp_path: Path,
) -> None:
    """#3023: a DIRTY node whose active ``resolve`` profile is refused.

    The governed resolve refuses before any runner, with conflicts already
    observed. The step returns the refused row's raw exit 2 with its category,
    so the base-integration hold names ``prompt_configuration_refused`` and the
    refusal reason, not a conflict nobody could resolve, and a second tick
    spends no second launch.
    """
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from support.prompt_profiles import activate_unchecked

    from kew.command_outcome import CommandOutcome
    from kew.graph.driver.review_launcher import sandbox_base_integrator
    from kew.inference_transport import ResolvedInferenceTransport
    from kew.prompt_addendum import PROMPT_CONFIGURATION_REFUSAL
    from kew.resolve import _run_conflicted_resolve
    from kew.worktree import ConflictInfo

    repo, pr_head = _diverged_repo(tmp_path)
    config = _config(tmp_path)
    facts = _facts(pr_head)
    statuses = {ITEM: DriverStatus.ADVERSARIAL_REVIEW}
    returned: list[CommandOutcome] = []
    runner = MagicMock()
    runner.name = "claude-code"
    runner.publication_style = "host"

    def governed_resolve(*, pr_number, config, store, on_conflict, expected_head_sha):
        assert expected_head_sha == pr_head
        conflict = ConflictInfo("merge", [BARREL], "main", "b" * 40)
        on_conflict(conflict)
        outcome = _run_conflicted_resolve(
            # Never created: the refusal's checkout cleanup has nothing to remove.
            worktree_dir=tmp_path / "absent-worktree",
            conflict_info=conflict,
            pr_number=pr_number,
            issue_number=int(ITEM),
            branch_name=BRANCH,
            base_branch="main",
            expected_head_sha=pr_head,
            pr_title="Barrel",
            config=config,
            store=store,
            producer_run_id=None,
            resolved=SimpleNamespace(runner=runner, model="claude-opus-5"),
            inference_transport=ResolvedInferenceTransport(
                mode="direct",
                requested_model="claude-opus-5",
                model="claude-opus-5",
                config_sha256="cfg",
                endpoint="https://api.anthropic.com",
            ),
            containment=SimpleNamespace(
                posture="sandboxed", image="kew-agent", contained=True, reason=None
            ),
            flow_run_id=f"run-resolve-{len(returned)}",
            item_id=ITEM,
        )
        returned.append(outcome)
        return outcome

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        activate_unchecked(store, "Keep it tidy.", "resolve", body_sha256="0" * 64)
        integrator = sandbox_base_integrator(
            config, store, epic_ref=EPIC, resolve=governed_resolve
        )
        first = _drive(
            store, repo, facts, statuses, config=config, base_integrator=integrator
        )
        [hold] = store.list_epic_holds(EPIC)
        statuses[ITEM] = DriverStatus.HELD
        second = _drive(
            store, repo, facts, statuses, config=config, base_integrator=integrator
        )
        [row] = store.list_runs_by_branch(BRANCH)

    assert first.held == [ITEM]
    assert second.held == [] and list(second.integrated) == []
    [outcome] = returned  # one launch attempt across both ticks
    assert outcome.kind == "not_run"
    assert outcome.category == PROMPT_CONFIGURATION_REFUSAL
    assert row.status == "refused"
    assert row.error_category == PROMPT_CONFIGURATION_REFUSAL
    assert outcome.exit_code == row.exit_code == 2
    assert hold.reason.startswith("base_integration_failed: ")
    assert f"{PROMPT_CONFIGURATION_REFUSAL}: {outcome.reason}" in hold.reason
    assert "does not match the approved digest" in hold.reason
    assert holds_node_out_of_frontier(hold.reason)
