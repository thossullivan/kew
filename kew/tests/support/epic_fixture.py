"""End-to-end scripted-drain fixture: demo-capture source for Phase A (#454).

Drives `advance()` through a fixed 5-node scenario (dispatch -> PR opened ->
approved-but-CI-blocked HELD -> CI-green merge -> dependents unblock, plus a
permanently FAILED branch) against a real SQLite store, faking only the
GitHub-shelling boundary that `merge_pass` reaches through for `gh pr merge`.

Reused by:
  - `tests/test_epic_journal_e2e.py` (this task's e2e test)
  - `scripts/make_demo_epic.py` (the Phase C demo-db generator)

Extends the shared conventions in `tests/support/driver_fakes.py`
(`FakeSource`, `FakeFactsReader`, `FakeSink`, `_facts`) rather than
inventing a parallel fake stack.

Node/edge topology (fixed): 101(A), 102(B), 103(C, deps A+B), 104(D, dep C),
105(E, dep A -- FAILS). `max_parallel=2`.

Note on tick count: dependents only become frontier-ready on the tick *after*
their dependency's merge lands (`_frontier_status` is computed from the
overlay snapshot taken *before* this tick's merge pass runs), so e.g. "E
dispatched" lands one tick after "A merged", not in the same tick. The e2e
test accounts for this; the exact tick count is not part of the contract,
only the end state and each node's per-tick journey.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from kew import subprocess_utils
from kew.graph.domain import Edge, Node, NodeStatus
from kew.graph.driver import tick as tick_module
from kew.graph.driver.tick import advance
from kew.run_store import EpicTick, RunRecord, SqliteRunStore
from support.driver_fakes import FakeFactsReader, FakeSink, FakeSource, _config, _facts

REPO = "demo-org/demo-epic"
_MAX_PARALLEL = 2
_APPROVERS = ["alice"]
_NODE_IDS = (101, 102, 103, 104, 105)


def _demo_source(epic_ref: str) -> FakeSource:
    """The fixed 5-node demo DAG: A, B, C(deps A,B), D(dep C), E(dep A)."""
    titles = {
        101: "Node A",
        102: "Node B",
        103: "Node C (deps A, B)",
        104: "Node D (dep C)",
        105: "Node E (dep A, fails)",
    }
    nodes = [
        Node(id=str(nid), title=titles[nid], status=NodeStatus.TODO, epic_ref=epic_ref)
        for nid in _NODE_IDS
    ]
    edges = [
        Edge("101", "103"),
        Edge("102", "103"),
        Edge("103", "104"),
        Edge("101", "105"),
    ]
    return FakeSource(nodes=nodes, edges=edges)


@dataclass
class _ScriptedWorld:
    """Mutable GitHub facts + the PR-number -> issue-number map the merge
    fake needs to know which issue to flip closed on a successful merge."""

    facts: FakeFactsReader
    issue_by_pr: dict[int, int] = field(default_factory=dict)

    def open_pr(
        self,
        issue_num: int,
        pr_number: int,
        *,
        review_decision: str | None = "REVIEW_REQUIRED",
        merge_state_status: str | None = "BLOCKED",
        approver: str | None = None,
    ) -> None:
        """Link `issue_num` to a freshly opened PR with the given raw facts."""
        self.issue_by_pr[pr_number] = issue_num
        self.facts.issue_facts_by_num[issue_num] = _facts(
            issue_num,
            state="OPEN",
            pr_number=pr_number,
            pr_url=f"https://github.com/{REPO}/pull/{pr_number}",
        )
        latest_reviews = (
            [
                {
                    "state": "APPROVED",
                    "submittedAt": "2026-07-01T00:00:00Z",
                    "author": {
                        "login": approver,
                        "is_bot": False,
                        "__typename": "User",
                    },
                }
            ]
            if approver
            else []
        )
        if approver:
            self.facts.permissions_by_login[approver] = "write"
        self.facts.pr_facts_by_num[pr_number] = {
            "number": pr_number,
            "state": "OPEN",
            "reviewDecision": review_decision,
            "mergeStateStatus": merge_state_status,
            "headRefOid": f"sha{issue_num}",
            "latestReviews": latest_reviews,
            "author": {"login": "dispatch-agent"},
            "commits": [],
        }

    def approve(self, pr_number: int, *, approver: str = "alice") -> None:
        pr = self.facts.pr_facts_by_num[pr_number]
        pr["reviewDecision"] = "APPROVED"
        pr["latestReviews"] = [
            {
                "state": "APPROVED",
                "submittedAt": "2026-07-01T00:00:00Z",
                "author": {
                    "login": approver,
                    "is_bot": False,
                    "__typename": "User",
                },
            }
        ]
        self.facts.permissions_by_login[approver] = "write"

    def set_merge_state(self, pr_number: int, merge_state_status: str) -> None:
        self.facts.pr_facts_by_num[pr_number]["mergeStateStatus"] = merge_state_status


def _fake_gh_run(world: _ScriptedWorld) -> Any:
    """Stand-in for the real stdlib `subprocess.run` that `merge_pass` shells
    `gh` through (via `kew.subprocess_utils.run`'s injectable-default seam,
    which is bound too early at import time to monkeypatch directly -- see
    `tests/test_epic_driver_gh_smoke.py` for the same pattern).

    Only `gh pr merge` is scripted. GitHub's own squash-merge auto-close (via
    a "Closes #N" reference) is simulated as a side effect here: flipping the
    linked issue's facts to CLOSED/COMPLETED, so `_verify_closed`'s first
    read already sees it closed and a separate `gh issue close` call never
    happens.
    """

    def _run(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        if argv[:3] == ["gh", "issue", "edit"]:
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        if argv[:3] != ["gh", "pr", "merge"]:
            raise AssertionError(f"unscripted gh call in demo fixture: {argv!r}")
        issue_num = world.issue_by_pr[int(argv[3])]
        current = world.facts.issue_facts_by_num[issue_num]
        world.facts.issue_facts_by_num[issue_num] = replace(
            current, state="CLOSED", state_reason="COMPLETED"
        )
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    return _run


def _finished_run(
    *, now: datetime, run_id: str, issue_num: int, status: str, cost: float
) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        issue_num=issue_num,
        branch=f"agent/issue-{issue_num}",
        status=status,
        exit_code=0 if status == "success" else 1,
        started_at=now - timedelta(minutes=1),
        finished_at=now,
        estimated_cost_usd=cost,
    )


def run_scripted_drain(
    db_path: Path, *, epic_ref: str = "demo-epic", monkeypatch: Any
) -> list[EpicTick]:
    """Drive `advance()` through the Phase A demo scenario against `db_path`.

    `monkeypatch` needs only `.setattr(target, name, value)` (the 3-arg
    form) and `.setitem(mapping, key, value)`, so both pytest's real
    `MonkeyPatch` and a tiny hand-rolled shim (see
    `scripts/make_demo_epic.py`) work here.

    Returns `store.list_epic_ticks(epic_ref)` -- the full journaled frame
    sequence, in tick order.
    """
    monkeypatch.setattr(tick_module, "preflight_tools", lambda: None)
    monkeypatch.setattr(tick_module, "remote_branch_exists", lambda branch: False)

    world = _ScriptedWorld(
        facts=FakeFactsReader(
            issue_facts_by_num={nid: _facts(nid, state="OPEN") for nid in _NODE_IDS}
        )
    )
    monkeypatch.setattr(subprocess, "run", _fake_gh_run(world))
    # resolve_cmd() only needs "gh" registered as *a* key; the fake above
    # never shells out to it. preflight_tools() is mocked out above so it
    # never populates this itself. setitem (not raw assignment) so pytest
    # teardown restores the process-global dict, matching driver_fakes.py's
    # _isolate_tool_paths convention.
    monkeypatch.setitem(subprocess_utils.TOOL_PATHS, "gh", "gh")

    store = SqliteRunStore(str(db_path))
    deps = _demo_source(epic_ref)
    sink = FakeSink()
    config = _config(stall_days=3)
    lock_path = db_path.parent / "epic-demo.lock"
    base = datetime.now(UTC)

    def _advance(i: int) -> None:
        advance(
            epic_ref,
            config=config,
            store=store,
            deps=deps,
            facts=world.facts,
            dispatch_sink=sink,
            max_parallel=_MAX_PARALLEL,
            now=base + timedelta(minutes=i),
            lock_path=lock_path,
            approvers=_APPROVERS,
            repo=REPO,
            interval_s=None,
        )

    def _save(i: int, run_id: str, issue_num: int, status: str, cost: float) -> None:
        # `finished_at` must land at (real) wall-clock "now", not `base +
        # timedelta(minutes=i)` (which is the *driver's* synthetic decision
        # clock, minutes ahead of real time): ticks are journaled with a
        # real `datetime.now(UTC)` tick_ts (see graph/driver/tick.py), so a
        # run "finished" minutes in the future would fall outside every
        # tick's `[window_start, tick_ts]` economics window forever and
        # every replay frame would show a $0 drain (#454 Phase B finding).
        # Capturing real "now" here keeps finished_at inside the window of
        # the tick that immediately follows this call (and every later one,
        # since `upper` only grows), while still landing after the epic's
        # first tick (`window_start`), which was journaled earlier in real
        # time. `i` is no longer used for the clock but is kept in the
        # signature for call-site readability/symmetry with `_advance`.
        store.save(
            _finished_run(
                now=datetime.now(UTC),
                run_id=run_id,
                issue_num=issue_num,
                status=status,
                cost=cost,
            )
        )

    # Tick 1: dispatch A, B (max_parallel=2).
    _advance(0)

    # Tick 2: A's run finishes success; A gets an OPEN PR -> in_review.
    _save(1, "run-101", 101, "success", 1.25)
    world.open_pr(101, 201)
    _advance(1)

    # Tick 3: A's PR APPROVED but mergeStateStatus BLOCKED -> held.
    world.approve(201)
    _advance(2)

    # Tick 4: A's PR goes CLEAN (CI green) -> merged -> done.
    world.set_merge_state(201, "CLEAN")
    _advance(3)

    # Tick 5: B finishes success + PR APPROVED + CLEAN -> merged -> done;
    # A's merge from the prior tick now reads DONE, so E becomes dispatchable.
    _save(4, "run-102", 102, "success", 2.10)
    world.open_pr(
        102,
        202,
        review_decision="APPROVED",
        merge_state_status="CLEAN",
        approver="alice",
    )
    _advance(4)

    # Tick 6: E's run finishes with status "failure" -> failed, stuck
    # "failed-terminal"; C becomes dispatchable (both A, B now read DONE).
    _save(5, "run-105", 105, "failure", 0.40)
    _advance(5)

    # Tick 7: C finishes + merged -> done.
    _save(6, "run-103", 103, "success", 0.90)
    world.open_pr(
        103,
        203,
        review_decision="APPROVED",
        merge_state_status="CLEAN",
        approver="alice",
    )
    _advance(6)

    # Tick 8: D becomes dispatchable (C now reads DONE).
    _advance(7)

    # Tick 9: D finishes + merged -> done. Overall STALLED: E's failure keeps
    # the drain from completing even though every other branch merged.
    _save(8, "run-104", 104, "success", 1.60)
    world.open_pr(
        104,
        204,
        review_decision="APPROVED",
        merge_state_status="CLEAN",
        approver="alice",
    )
    _advance(8)

    ticks = store.list_epic_ticks(epic_ref)
    store.close()
    return ticks
