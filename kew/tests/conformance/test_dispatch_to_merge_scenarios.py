"""Dispatch-to-merge scenarios that combine features (#2487).

The rest of this pack holds contract tables: one feature, many shapes. A suite
built only from those saturates — each feature passes alone while the seams
between them go untested. These scenarios walk one path end to end on real
code and combine what the tables keep apart: the epic graph, the tick's
reservations and journal, the governed review verdict, the merge pass, the
dependent's release, and the budget-stop collection ladder.

Only two things are fake, and both are boundaries kew does not own:

* the forge — :class:`support.gh_fakes.FakeForge`, reached through
  :class:`support.gh_fakes.GhProcessBoundary`, so every ``gh`` argv kew builds
  leaves through ``subprocess_utils.run`` exactly as in production. Its one
  external effect, a merge, moves a real ref in a real bare origin;
* the runner — :class:`_FakeRunner`, the epic's dispatch sink, which leaves
  what an implementation agent's run leaves: a real commit on a real branch and
  the run rows a dispatch child writes.

Assertions read observable state only: git refs and commits, store rows, CLI
output and exit codes, and the forge calls that stand for external effects.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from support import driver_fakes as _fx
from support.driver_fakes import EPIC
from support.gh_fakes import FakeForge, GhProcessBoundary
from typer.testing import CliRunner

from kew import subprocess_utils
from kew.cli import app
from kew.config import DispatchConfig
from kew.dispatch_core import _collect_result, _DispatchContext
from kew.graph.adapters.github_convention import GitHubConventionSource
from kew.graph.driver.facts import GitHubFactsReader
from kew.graph.driver.result import TerminalSignal, TickResult
from kew.graph.driver.tick import advance
from kew.review_audit import emit_review_verdict
from kew.run_store import ADVERSARIAL_REVIEW_RUN_KIND, RunRecord, SqliteRunStore
from kew.tracking import DispatchResult

# advance() preflights tools into the global allowlist; restore it afterwards.
_isolate_tool_paths = _fx._isolate_tool_paths

REPO = "o/r"
APPROVER = "alice"


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )
    return done.stdout.strip()


def _commits(cwd: Path, ref: str) -> list[str]:
    """Subjects on *ref*, newest first, as ``git log`` prints them."""
    return _git(cwd, "log", "--format=%s", ref).splitlines()


@dataclass
class World:
    """One operator checkout, its bare origin, the ledger and the forge."""

    checkout: Path
    origin: Path
    store: SqliteRunStore
    forge: FakeForge
    config: DispatchConfig
    lock_path: Path


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[World]:
    """A real repository whose ``origin`` is a local bare one, run from inside.

    The tick lists remote branches with a real ``git ls-remote origin`` from the
    working directory, so the scenario runs in this checkout and nothing about
    git is patched. The ledger sits at the default config's ``dispatch.db`` so
    the CLI, loading its own config, opens the same rows the tick wrote.
    """
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    checkout = tmp_path / "checkout"
    _git(tmp_path, "clone", "-q", str(origin), str(checkout))
    _git(checkout, "config", "user.email", "kew@example.invalid")
    _git(checkout, "config", "user.name", "kew")
    (checkout / "README.md").write_text("base\n", encoding="utf-8")
    _git(checkout, "add", "README.md")
    _git(checkout, "commit", "-q", "-m", "base")
    _git(checkout, "push", "-q", "origin", "main")
    monkeypatch.chdir(checkout)

    forge = FakeForge(origin=origin)
    forge.permissions[APPROVER] = "write"
    monkeypatch.setattr(
        subprocess_utils,
        "subprocess",
        GhProcessBoundary(forge, tool_paths=subprocess_utils.TOOL_PATHS),
    )
    config = DispatchConfig()
    config.project.repo = REPO
    log_dir = Path(config.logging.dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    store = SqliteRunStore(str(log_dir / "dispatch.db"))
    yield World(
        checkout=checkout,
        origin=origin,
        store=store,
        forge=forge,
        config=config,
        lock_path=tmp_path / "epic.lock",
    )
    store.close()


class _FakeRunner:
    """The dispatch sink: an agent run, reduced to the state it leaves behind.

    ``dispatch`` cuts ``agent/issue-N`` from origin's base as it stands at that
    moment, commits a real file, pushes it, and writes the pending run row a
    detached dispatch child writes first. ``finish`` is the child exiting
    cleanly: the row goes terminal and the forge gains the PR the host opened.
    """

    def __init__(self, world: World) -> None:
        self.world = world
        self.dispatched: list[str] = []
        self.heads: dict[str, str] = {}

    def dispatch(self, item_id: str) -> bool:
        work = self.world.checkout
        self.dispatched.append(item_id)
        branch = f"agent/issue-{item_id}"
        _git(work, "fetch", "-q", "origin")
        _git(work, "checkout", "-q", "-B", branch, "origin/main")
        (work / f"node_{item_id}.txt").write_text(f"{item_id}\n", "utf-8")
        _git(work, "add", f"node_{item_id}.txt")
        _git(work, "commit", "-q", "-m", f"feat: node {item_id}")
        _git(work, "push", "-q", "origin", branch)
        self.heads[item_id] = _git(work, "rev-parse", "HEAD")
        self.world.store.save(_run_row(item_id, "pending"))
        return True

    def finish(self, item_id: str) -> dict[str, Any]:
        pr = self.world.forge.open_pr(
            issue=int(item_id),
            branch=f"agent/issue-{item_id}",
            head=self.heads[item_id],
        )
        self.world.store.save(_run_row(item_id, "success", pr_url=pr["url"]))
        return pr

    def review(self, item_id: str, *, verdict: str) -> None:
        """The fresh-context reviewer's run: its own run row, then its verdict
        on the head it read. The row is what makes the node policy-covered — a
        node no reviewer ever ran on is grandfathered past the review gate."""
        now = datetime.now(UTC)
        self.world.store.save(
            RunRecord(
                run_id=f"review-{item_id}",
                issue_num=int(item_id),
                item_id=item_id,
                branch=f"agent/issue-{item_id}",
                status="success",
                exit_code=0,
                started_at=now,
                finished_at=now,
                run_kind=ADVERSARIAL_REVIEW_RUN_KIND,
            )
        )
        self.world.store.record_review_round(
            EPIC, item_id, head_sha=self.heads[item_id], verdict=verdict
        )


def _run_row(
    item_id: str,
    status: str,
    *,
    pr_url: str | None = None,
    exit_code: int = 0,
    error_category: str = "",
) -> RunRecord:
    now = datetime.now(UTC)
    return RunRecord(
        run_id=f"run-{item_id}",
        issue_num=int(item_id),
        item_id=item_id,
        branch=f"agent/issue-{item_id}",
        status=status,
        exit_code=exit_code,
        started_at=now,
        finished_at=None if status == "pending" else now,
        pr_url=pr_url,
        error_category=error_category,
    )


def _two_node_epic(world: World) -> None:
    """Issue 2 depends on issue 1; both carry the epic's label on the forge."""
    world.forge.open_issue(1, epic=EPIC)
    world.forge.open_issue(2, epic=EPIC, depends_on=(1,))


def _tick(
    world: World,
    runner: _FakeRunner,
    *,
    approvers: list[str],
) -> TickResult:
    return advance(
        EPIC,
        config=world.config,
        store=world.store,
        deps=GitHubConventionSource(REPO),
        facts=GitHubFactsReader(REPO, store=world.store, host="github.com"),
        dispatch_sink=runner,
        max_parallel=5,
        now=datetime.now(UTC),
        lock_path=world.lock_path,
        approvers=approvers,
        repo=REPO,
    )


def _approve_after_verdict(world: World, pr: dict[str, Any]) -> None:
    """A human approval that postdates the verdict, as the merge gate needs."""
    approved_at = datetime.now(UTC) + timedelta(seconds=1)
    world.forge.approve(
        pr["number"],
        login=APPROVER,
        at=approved_at.isoformat().replace("+00:00", "Z"),
    )


def _journal(store: SqliteRunStore) -> list[dict[str, Any]]:
    """Each journaled tick's actions, oldest first — the ledger's own record."""
    return [tick.actions for tick in store.list_epic_ticks(EPIC)]


def _epic_status() -> dict[str, Any]:
    """`kew epic status --json`, as an operator runs it: its own config, the
    same ledger, a live read of the same forge. Exit 0 is part of the contract.
    """
    result = CliRunner().invoke(app, ["epic", "status", EPIC, "--json"])
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


class TestAnEpicOfTwoDependentNodes:
    """Dispatch, review READY, merge — and only then does the dependent go."""

    def test_the_dependent_dispatches_only_after_the_first_node_merges(
        self, world: World
    ) -> None:
        world.config.review.adversarial = True
        world.config.review.blocking = True
        _two_node_epic(world)
        runner = _FakeRunner(world)
        store = world.store

        # Tick 1: only the root is dispatchable; the dependent has no row at all.
        assert _tick(world, runner, approvers=[APPROVER]).dispatched == ("1",)
        assert [r.item_id for r in store.list_epic_reservations(EPIC)] == ["1"]
        assert store.list_by_item("2") == []

        # The run publishes, the fresh-context reviewer says READY on that exact
        # head, and a human approves after the verdict.
        pr = runner.finish("1")
        runner.review("1", verdict="READY")
        _approve_after_verdict(world, pr)

        # Tick 2 merges the root. The dependent is still unreserved and has no
        # run: the merge and the release are not the same instant.
        merged = _tick(world, runner, approvers=[APPROVER])
        assert merged.merged == ("1",)
        assert merged.dispatched == ()
        assert store.list_by_item("2") == []
        assert "2" not in {r.item_id for r in store.list_epic_reservations(EPIC)}
        assert world.forge.merges() == [
            [
                "gh",
                "pr",
                "merge",
                str(pr["number"]),
                "--repo",
                REPO,
                "--squash",
                "--match-head-commit",
                runner.heads["1"],
            ]
        ]
        assert _git(world.origin, "rev-parse", "main") == runner.heads["1"]

        # Tick 3 releases the dependent.
        assert _tick(world, runner, approvers=[APPROVER]).dispatched == ("2",)
        assert [r.item_id for r in store.list_epic_reservations(EPIC)] == ["2"]
        assert [r.status for r in store.list_by_item("2")] == ["pending"]
        assert runner.dispatched == ["1", "2"]

        # The ledger's journal orders it: dispatch 1, merge 1, and only on a
        # later tick, dispatch 2.
        journal = _journal(store)
        merge_tick = next(i for i, a in enumerate(journal) if "1" in a["merged"])
        dependent_ticks = [i for i, a in enumerate(journal) if "2" in a["dispatched"]]
        assert journal[0]["dispatched"] == ["1"]
        assert dependent_ticks and min(dependent_ticks) > merge_tick

        # And the dependent was cut from the base the merge moved: its branch
        # carries node 1's commit under its own.
        assert _commits(world.origin, "agent/issue-2") == [
            "feat: node 2",
            "feat: node 1",
            "base",
        ]
        status = _epic_status()
        assert status["done"] == ["1"]
        assert {n["id"]: n["status"] for n in status["nodes"]} == {
            "1": "done",
            "2": "in_progress",
        }
        assert status["signal"] == "progressing"
        assert world.forge.unmodelled == []

    def test_a_not_ready_verdict_never_releases_the_dependent(
        self, world: World
    ) -> None:
        """The control: the same approved, green PR, but the reviewer said
        NEEDS_CHANGES on its head. Blocking review sends the node to
        remediation instead of the merge, so the dependent never dispatches:
        the READY round is what released it above.
        """
        world.config.review.adversarial = True
        world.config.review.blocking = True
        _two_node_epic(world)
        runner = _FakeRunner(world)

        _tick(world, runner, approvers=[APPROVER])
        pr = runner.finish("1")
        runner.review("1", verdict="NEEDS_CHANGES")
        _approve_after_verdict(world, pr)
        for _ in range(2):
            assert _tick(world, runner, approvers=[APPROVER]).dispatched == ()

        assert world.forge.merges() == []
        assert runner.dispatched == ["1"]
        assert world.store.list_by_item("2") == []
        assert _git(world.origin, "rev-parse", "main") != runner.heads["1"]
        assert world.forge.unmodelled == []


def _attest(
    world: World, runner: _FakeRunner, pr: dict[str, Any], verdict: str
) -> None:
    """An ``adversarial_review_verdict`` on node 1's head, as a reviewer that
    ran outside this epic's rounds (a hand ``kew review``) attests one."""
    emit_review_verdict(
        world.store,
        world.config.review,
        pr_number=pr["number"],
        item_id="1",
        head_sha=runner.heads["1"],
        review_round=1,
        verdict=verdict,
    )


def _merge_blocks(store: SqliteRunStore) -> list[str]:
    """The reasons the merge pass attested for refusing node 1, oldest first."""
    events = store.list_audit_events(None, event="review_merge_blocked", item_id="1")
    return [e.payload["reason"] for e in reversed(events)]


class TestTheMergeVerdictRuleInTheDrive:
    """The epic tick's merge pass applies the one merge verdict rule (#3196) on
    top of whatever its review mode asks: a NEEDS_CHANGES attested as the head's
    newest verdict stops the merge through the fake forge."""

    def _published(
        self, world: World, *, blocking: bool, round_verdict: str | None
    ) -> tuple[_FakeRunner, dict[str, Any]]:
        world.config.review.adversarial = True
        world.config.review.blocking = blocking
        _two_node_epic(world)
        runner = _FakeRunner(world)
        _tick(world, runner, approvers=[APPROVER])
        pr = runner.finish("1")
        if round_verdict is not None:
            runner.review("1", verdict=round_verdict)
        _approve_after_verdict(world, pr)
        return runner, pr

    @pytest.mark.parametrize("blocking", [True, False], ids=["blocking", "advisory"])
    def test_an_attested_needs_changes_stops_the_merge_over_a_ready_round(
        self, world: World, blocking: bool
    ) -> None:
        runner, pr = self._published(world, blocking=blocking, round_verdict="READY")
        _attest(world, runner, pr, "NEEDS_CHANGES")

        result = _tick(world, runner, approvers=[APPROVER])

        assert result.merged == ()
        assert world.forge.merges() == []
        assert _git(world.origin, "rev-parse", "main") != runner.heads["1"]
        assert _merge_blocks(world.store) == ["merge-verdict-needs-changes"]
        # Waiting on a re-review, not a broken pipeline.
        assert result.signal is TerminalSignal.AWAITING_MERGES
        assert world.forge.unmodelled == []

    def test_a_grandfathered_node_with_an_attested_needs_changes_does_not_merge(
        self, world: World
    ) -> None:
        """PRE_POLICY: no round, no review run, approved before review applied.
        The round rule waives it; the verdict rule does not."""
        runner, pr = self._published(world, blocking=True, round_verdict=None)
        _attest(world, runner, pr, "NEEDS_CHANGES")

        result = _tick(world, runner, approvers=[APPROVER])

        assert result.merged == ()
        assert world.forge.merges() == []
        assert _merge_blocks(world.store) == ["merge-verdict-needs-changes"]

    @pytest.mark.parametrize("blocking", [True, False], ids=["blocking", "advisory"])
    def test_a_newer_ready_verdict_merges(self, world: World, blocking: bool) -> None:
        runner, pr = self._published(world, blocking=blocking, round_verdict="READY")
        _attest(world, runner, pr, "NEEDS_CHANGES")
        _attest(world, runner, pr, "READY")

        result = _tick(world, runner, approvers=[APPROVER])

        assert result.merged == ("1",)
        assert len(world.forge.merges()) == 1
        assert _git(world.origin, "rev-parse", "main") == runner.heads["1"]
        assert _merge_blocks(world.store) == []


class TestABudgetStopMidRun:
    """The watchdog stops a run part-way: the work survives, the node is held."""

    def _stop_mid_run(self, world: World) -> Any:
        """The runner's side of a budget stop, then kew's real collection of it.

        The agent committed one step and was writing the next when the live
        budget watchdog stopped it: one commit on the branch, one file still
        uncommitted in the checkout, and the ``budget_stop`` run row.
        """
        config = world.config
        config.publication.publish = "checkout"
        config.sandbox.dangerously_skip_containment = True
        subprocess_utils.register_tools()
        work = world.checkout
        base_sha = _git(work, "rev-parse", "HEAD")
        branch = "agent/issue-1"
        _git(work, "checkout", "-q", "-b", branch)
        (work / "step_one.txt").write_text("done\n", "utf-8")
        _git(work, "add", "step_one.txt")
        _git(work, "commit", "-q", "-m", "feat: step one")
        (work / "step_two.txt").write_text("half-written\n", "utf-8")
        world.store.save(
            _run_row("1", "budget_stop", exit_code=143, error_category="budget_stop")
        )
        log = work.parent / "run.log"
        log.write_text("", "utf-8")
        ctx = _DispatchContext(
            config=config,
            store=world.store,
            notifier=None,
            force_budget=False,
            approved=False,
            interactive=False,
            model_override=None,
            merge_context=None,
            epic_ref=EPIC,
            provider="github",
            issue_num=1,
            issue_title="node one",
            issue_body="",
            complexity="medium",
            item_id="1",
            branch=branch,
            start_monotonic=0.0,
            refuse_rollback=lambda _reason: None,
            terminal_failure=None,
            run_kind="dispatch",
            worktree_dir=work,
            run_id="run-1",
            publication_base_sha=base_sha,
            allow_unrecorded=True,
        )
        result = DispatchResult(
            exit_code=143,
            issue_num=1,
            log_file=log,
            session_id="sess-1",
            error_category="budget_stop",
            budget_exceeded=True,
            run_id="run-1",
        )
        return _collect_result(
            ctx, result=result, worktree_dir=work, run_sandboxed_path=False
        )

    def test_budget_stop_leaves_the_wip_commit_on_the_branch(
        self, world: World
    ) -> None:
        world.forge.open_issue(1)

        outcome = self._stop_mid_run(world)

        assert outcome.exit_code == 143
        # `git log` on the branch, not a mock: kew committed the half-written
        # step on top of the agent's own commit, and nothing is left behind.
        subjects = _commits(world.checkout, "agent/issue-1")
        assert subjects[1:] == ["feat: step one", "base"]
        assert subjects[0].startswith("feat(#1)")
        wip_files = _git(
            world.checkout, "show", "--name-only", "--format=", "agent/issue-1"
        )
        assert wip_files.splitlines() == ["step_two.txt"]
        assert _git(world.checkout, "status", "--porcelain") == ""

    def test_budget_stop_holds_the_node_with_its_reason(self, world: World) -> None:
        _two_node_epic(world)
        runner = _FakeRunner(world)

        self._stop_mid_run(world)

        # The forge was told why, in the label and the comment kew wrote.
        assert world.config.labels.budget_stop in world.forge.labels(1)
        comments = [c["body"] for c in world.forge.issues[1]["comments"]]
        assert any(
            "graceful budget stop (WIP committed to branch)" in body
            for body in comments
        )

        # The next ticks hold it there: never re-dispatched, its dependent never
        # released, and the reason names the budget stop.
        for _ in range(2):
            result = _tick(world, runner, approvers=[])
            assert result.dispatched == ()
            assert [(s.node_id, s.reason) for s in result.stuck] == [
                ("1", "failed-terminal (exit 143, budget_stop)")
            ]
        assert runner.dispatched == []
        assert world.store.list_epic_reservations(EPIC) == []
        assert [r.status for r in world.store.list_by_item("1")] == ["budget_stop"]
        assert world.store.list_by_item("2") == []
        assert all(a["dispatched"] == [] for a in _journal(world.store))
        status = _epic_status()
        assert status["stuck"] == [
            {
                "id": "1",
                "reason": "failed-terminal (exit 143, budget_stop)",
                "blast_radius": ["2"],
            }
        ]
        assert {n["id"]: n["status"] for n in status["nodes"]} == {
            "1": "failed",
            "2": "todo",
        }
        assert world.forge.unmodelled == []


@pytest.mark.skip(
    reason=(
        "B4 of the test-integrity epic (#2477) has not landed: the reviewer "
        "context carries no integrity flag for a fix round that edits only "
        "tests, so there is nothing observable to assert yet"
    )
)
def test_a_fix_round_that_edits_only_tests_flags_the_reviewer_context() -> None:
    """Scenario 3 (#2487): a review fix round whose diff touches only tests.

    Once B4 lands: dispatch a node, record a NOT_READY round, let the fake
    runner's fix commit edit only files under ``tests/``, and assert the next
    reviewer's context — as written for the reviewer, not as a mock saw it —
    carries the integrity flag.
    """
