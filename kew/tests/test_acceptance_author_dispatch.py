"""A dispatch prepares the acceptance contract before the implementer (#2767).

These drive ``dispatch_core._launch_at_contract``, the one point both
workspaces sequence the author and the implementer through. The implementer
launch, the author launch and the red-at-base pytest are handed in as
arguments; nothing in ``kew.dispatch_core`` is patched except ``run_agent``,
the imported process boundary of a paid run.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from support.prompt_profiles import activate_unchecked
from support.pytest_here import run_pytest_here

from kew import dispatch_core, worktree
from kew.acceptance_author import AuthorRequest
from kew.audit import Actor
from kew.budget import BudgetExceeded, BudgetStatus
from kew.config import DispatchConfig
from kew.prompt_addendum import ProfileAddendumNotRenderedError
from kew.run_store import SqliteRunStore
from kew.runners.registry import ResolvedRoute, resolve_runner
from kew.service import prompt_profiles as profiles
from kew.subprocess_utils import TOOL_PATHS
from kew.tracking import DispatchResult
from kew.triage.schema import AcceptanceCriterion, ItemPlan

ISSUE = 7
EPIC = "epic-1"
CONTRACT = f"kew/tests/acceptance/test_issue_{ISSUE}.py"
RED_TEST = """\
from pathlib import Path


def test_ac1():
    assert "VALUE = 2" in Path("kew/feature.py").read_text()
"""


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """A node checkout on ``agent/issue-7`` at its base commit."""
    git = shutil.which("git")
    assert git is not None
    TOOL_PATHS["git"] = git
    repo = tmp_path / "checkout"
    (repo / "kew").mkdir(parents=True)
    (repo / "kew" / "feature.py").write_text("VALUE = 1\n")
    _git(repo, "init", "-q", "-b", f"agent/issue-{ISSUE}")
    _git(repo, "config", "user.email", "kew@example.invalid")
    _git(repo, "config", "user.name", "kew")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    # Bound like a minted checkout, so a launch past admission reaches the
    # process boundary rather than the checkout-identity refusal.
    worktree.write_checkout_generation(repo)
    return repo


@pytest.fixture
def store(tmp_path: Path) -> Any:
    with SqliteRunStore(str(tmp_path / "runs.db")) as opened:
        plan = ItemPlan(
            acceptance_criteria=[
                AcceptanceCriterion(id="AC1", statement="value is 2", kind="test")
            ]
        )
        opened.record_item_plan(
            str(ISSUE),
            body_digest="digest",
            source="extracted",
            plan_sha="plan-sha",
            plan_json=plan.model_dump_json(),
            criteria_count=1,
        )
        yield opened


def _ctx(store: Any, tmp_path: Path) -> Any:
    config = DispatchConfig()
    config.logging.dir = str(tmp_path / "logs")
    return dispatch_core._DispatchContext(
        config=config,
        store=store,
        notifier=None,
        force_budget=False,
        approved=True,
        interactive=False,
        model_override=None,
        merge_context=None,
        epic_ref=EPIC,
        provider="github",
        issue_num=ISSUE,
        issue_title="Value is two",
        issue_body="## Acceptance\n- value is 2",
        complexity="simple",
        item_id=str(ISSUE),
        branch=f"agent/issue-{ISSUE}",
        start_monotonic=0.0,
        refuse_rollback=lambda _reason: None,
        terminal_failure=None,
        run_kind="dispatch",
    )


def _route(ctx: Any) -> ResolvedRoute:
    return resolve_runner(ctx.config, complexity=ctx.complexity)


def test_the_author_launches_before_the_implementer_which_starts_at_the_contract(
    store: Any, checkout: Path, tmp_path: Path
) -> None:
    ctx = _ctx(store, tmp_path)
    base = _git(checkout, "rev-parse", "HEAD")
    launches: list[tuple[str, str]] = []

    def author(
        _ctx: Any,
        request: AuthorRequest,
        *,
        admit_budget_check: Any,
        sandbox_posture: str | None,
    ) -> str:
        launches.append(("author", _git(request.worktree, "rev-parse", "HEAD")))
        target = request.worktree / CONTRACT
        target.parent.mkdir(parents=True)
        target.write_text(RED_TEST)
        return "author-run-1"

    def implementer() -> DispatchResult:
        launches.append(("implementer", _git(checkout, "rev-parse", "HEAD")))
        return DispatchResult(
            exit_code=0, issue_num=ISSUE, log_file=tmp_path / "impl.log"
        )

    result = dispatch_core._launch_at_contract(
        ctx,
        checkout,
        _route(ctx),
        admit_budget_check=None,
        sandbox_posture="native",
        launch_implementer=implementer,
        launch_author=author,
        run_test=run_pytest_here,
    )

    contract = store.current_acceptance_contract(EPIC, str(ISSUE))
    assert contract is not None
    assert contract.author_run_id == "author-run-1"
    assert result.exit_code == 0
    assert launches == [("author", base), ("implementer", contract.commit)]
    assert _git(checkout, "rev-parse", f"{contract.commit}^") == base


class ProcessSpawned(RuntimeError):
    """A paid agent process was started."""


def test_an_author_run_the_budget_admission_refuses_launches_nothing(
    store: Any,
    checkout: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _ctx(store, tmp_path)
    base = _git(checkout, "rev-parse", "HEAD")
    admissions: list[str] = []
    spawned: list[object] = []
    implementer_launches: list[str] = []

    def refuse_budget() -> None:
        admissions.append("refused")
        raise BudgetExceeded(
            "daily",
            1.0,
            2.0,
            BudgetStatus(
                issue_spent_usd=0,
                issue_limit_usd=15.0,
                daily_spent_usd=2.0,
                daily_limit_usd=1.0,
                weekly_spent_usd=2.0,
                weekly_limit_usd=200.0,
            ),
        )

    def implementer() -> DispatchResult:
        implementer_launches.append("implementer")
        return DispatchResult(
            exit_code=0, issue_num=ISSUE, log_file=tmp_path / "impl.log"
        )

    def spawn(*args: object, **kwargs: object) -> None:
        spawned.append(args)
        raise ProcessSpawned

    # The paid process boundary of every tracked run, the author's included.
    monkeypatch.setattr(dispatch_core, "run_agent", spawn)
    with pytest.raises(BudgetExceeded):
        dispatch_core._launch_at_contract(
            ctx,
            checkout,
            _route(ctx),
            admit_budget_check=refuse_budget,
            sandbox_posture="native",
            launch_implementer=implementer,
        )

    assert admissions == ["refused"]
    assert spawned == []
    assert implementer_launches == []
    assert store.current_acceptance_contract(EPIC, str(ISSUE)) is None
    assert store.list_acceptance_contracts(EPIC, str(ISSUE)) == []
    assert _git(checkout, "rev-parse", "HEAD") == base
    assert not (checkout / CONTRACT).exists()


def test_a_prompt_profile_refusal_comes_before_the_paid_author(
    store: Any,
    checkout: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An active profile beside a custom ``[prompt] template`` would refuse the
    implementer's render; it is refused before the author is paid for, and the
    refusal holds the node instead of failing it (#3022)."""
    activate_unchecked(store, "Prefer small commits.")
    store.reserve_epic_node_item(EPIC, str(ISSUE), "run-7")
    ctx = _ctx(store, tmp_path)
    template = tmp_path / "custom.md.j2"
    template.write_text("{{ issue_body_fenced }}\n")
    ctx.config.prompt.template = str(template)
    ctx.run_id = ctx.admitted_run_id = ctx.expected_reservation_run_id = "run-7"
    rollbacks: list[str] = []
    ctx.refuse_rollback = rollbacks.append
    base = _git(checkout, "rev-parse", "HEAD")
    authors: list[object] = []
    implementer_launches: list[str] = []

    def author(*args: object, **kwargs: object) -> str:
        authors.append(args)
        return "author-run-1"

    def implementer() -> DispatchResult:
        implementer_launches.append("implementer")
        return DispatchResult(
            exit_code=0, issue_num=ISSUE, log_file=tmp_path / "impl.log"
        )

    # The paid process boundary of every tracked run, the author's included.
    monkeypatch.setattr(dispatch_core, "run_agent", author)
    with pytest.raises(ProfileAddendumNotRenderedError) as refused:
        dispatch_core._launch_at_contract(
            ctx,
            checkout,
            _route(ctx),
            admit_budget_check=None,
            sandbox_posture="native",
            launch_implementer=implementer,
            launch_author=author,
            run_test=run_pytest_here,
        )
    outcome = dispatch_core._classify_prelaunch_refusal(
        ctx, dispatch_core._LaunchSetup(), dispatch_core._EarlyRunInfo(), refused.value
    ).early

    assert authors == []
    assert implementer_launches == []
    assert store.current_acceptance_contract(EPIC, str(ISSUE)) is None
    assert store.list_acceptance_contracts(EPIC, str(ISSUE)) == []
    assert _git(checkout, "rev-parse", "HEAD") == base
    assert not (checkout / CONTRACT).exists()
    assert outcome is not None
    assert outcome.category == "prompt_configuration_refused"
    assert not outcome.launched
    # The claim goes back with the reason that names the profile and remedy.
    assert "dispatch prompt profile v1" in rollbacks[0]
    assert "retire the profile or remove [prompt] template" in rollbacks[0]
    rows = store.list_all()
    assert [(row.run_id, row.status) for row in rows] == [("run-7", "refused")]


def test_the_implementer_runs_under_the_addendum_read_before_the_author(
    store: Any,
    checkout: Path,
    tmp_path: Path,
) -> None:
    """One read per dispatch: a profile retired while the author runs does not
    change what the implementer renders and records (#3022)."""
    activate_unchecked(store, "Prefer small commits.")
    ctx = _ctx(store, tmp_path)
    ctx.config.approval.approvers = ["alice"]
    seen: list[object] = []

    def author(
        _ctx: Any,
        request: AuthorRequest,
        *,
        admit_budget_check: Any,
        sandbox_posture: str | None,
    ) -> str:
        profiles.retire(
            store,
            role="dispatch",
            generation=profiles.current_generation(store, "dispatch"),
            config=ctx.config,
            actor=Actor("human", "alice"),
        )
        target = request.worktree / CONTRACT
        target.parent.mkdir(parents=True)
        target.write_text(RED_TEST)
        return "author-run-1"

    def implementer() -> DispatchResult:
        # What the host and contained launches render and persist.
        seen.append(dispatch_core._dispatch_addendum(ctx))
        return DispatchResult(
            exit_code=0, issue_num=ISSUE, log_file=tmp_path / "impl.log"
        )

    dispatch_core._launch_at_contract(
        ctx,
        checkout,
        _route(ctx),
        admit_budget_check=None,
        sandbox_posture="native",
        launch_implementer=implementer,
        launch_author=author,
        run_test=run_pytest_here,
    )

    assert profiles.launch_addendum(store) is None
    assert [getattr(a, "version", None) for a in seen] == [1]
