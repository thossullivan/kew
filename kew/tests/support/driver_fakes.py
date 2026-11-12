"""Shared fakes/fixtures for tick-orchestrator tests (#454 Phase A).

Extracted from `test_driver_tick.py` (Task 10) so `test_driver_tick_journal.py`
(Task A3) can reuse the exact same seam conventions without inventing a
parallel fake stack. Every seam is a fake except the run-store (a real
`SqliteRunStore` on a tmp db) and `remote_branch_exists`, which is
monkeypatched to avoid a real `git ls-remote` network call.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from kew import subprocess_utils
from kew.config import DispatchConfig, EpicConfig
from kew.graph.domain import Edge, Node, NodeStatus
from kew.graph.driver.facts import NodeFacts, RepositoryPermission
from kew.graph.driver.result import TickResult
from kew.graph.driver.tick import advance
from kew.run_store import SqliteRunStore

REPO = "o/r"
EPIC = "epic/dag-1"
NOW = datetime(2026, 7, 1, tzinfo=UTC)


class FakeSource:
    """In-memory DependencySource (mirrors test_graph_build.py's FakeSource)."""

    def __init__(
        self,
        nodes: list[Node],
        edges: list[Edge],
        statuses: dict[str, NodeStatus | None] | None = None,
    ) -> None:
        self._nodes = nodes
        self._edges = edges
        self._statuses = statuses or {}

    def list_epic_nodes(self, epic_ref: str) -> list[Node]:
        return list(self._nodes)

    def list_edges(self, nodes: list[Node]) -> list[Edge]:
        return list(self._edges)

    def status_of(self, node_id: str) -> NodeStatus | None:
        return self._statuses.get(node_id)


def _node(nid: str, *, status: NodeStatus = NodeStatus.TODO) -> Node:
    return Node(id=nid, title=f"t{nid}", status=status, epic_ref=EPIC)


def _facts(
    issue_num: int,
    *,
    state: str = "OPEN",
    state_reason: str | None = None,
    labels: frozenset[str] = frozenset(),
    pr_number: int | None = None,
    pr_url: str | None = None,
    pr_state: str | None = None,
    pr_pushed_at: str | None = None,
    admission_status: NodeStatus = NodeStatus.TODO,
    head_oid: str | None = None,
) -> NodeFacts:
    return NodeFacts(
        issue_num=issue_num,
        state=state,
        state_reason=state_reason,
        labels=labels,
        pr_number=pr_number,
        pr_url=pr_url,
        pr_state=pr_state,
        review_decision=None,
        merge_state_status=None,
        head_oid=head_oid,
        pr_pushed_at=pr_pushed_at,
        admission_status=admission_status,
    )


@dataclass
class FakeFactsReader:
    """Scripted GitHubFactsReader: per-issue NodeFacts + per-PR raw dicts."""

    issue_facts_by_num: dict[int, NodeFacts] = field(default_factory=dict)
    pr_facts_by_num: dict[int, dict[str, Any]] = field(default_factory=dict)
    permissions_by_login: dict[str, str] = field(default_factory=dict)
    # What every PR head's own check runs say (#2033).
    checks: tuple[str, str] = ("success", "CI green")

    def issue_facts(self, issue_num: int) -> NodeFacts:
        return self.issue_facts_by_num[issue_num]

    def pr_facts(self, pr_number: int) -> dict[str, Any]:
        return self.pr_facts_by_num.get(pr_number, {})

    def required_checks_green(self, base: str, pr: dict[str, Any]) -> bool:
        return pr.get("mergeStateStatus") in {"CLEAN", "HAS_HOOKS"}

    def head_checks(self, sha: str, gated_checks: Any) -> tuple[str, str]:
        return self.checks

    def repository_permission(self, login: str) -> RepositoryPermission | None:
        permission = self.permissions_by_login.get(login)
        if permission is None:
            return None
        return RepositoryPermission(login, "User", permission)


class FakeSink:
    """Records dispatched item ids; never spawns anything."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.continued: list[tuple[str, str]] = []

    def dispatch(self, item_id: str) -> bool:
        self.calls.append(item_id)
        return True

    def continue_claimed(self, item_id: str, continuation_run_id: str) -> bool:
        """Record one claimed continuation the driver handed off (#2964)."""
        self.continued.append((item_id, continuation_run_id))
        return True


@pytest.fixture
def store(tmp_path: Path) -> Any:
    s = SqliteRunStore(str(tmp_path / "run.db"))
    yield s
    s.close()


@pytest.fixture(autouse=True)
def _no_real_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """No dispatch child in these tests has actually pushed a branch."""
    monkeypatch.setattr(
        "kew.graph.driver.tick.remote_branch_exists", lambda branch: False
    )


@pytest.fixture(autouse=True)
def _isolate_tool_paths() -> Any:
    # advance() calls preflight_tools(), which mutates the global
    # subprocess_utils.TOOL_PATHS with no cleanup. Save/restore so these
    # tests don't leak tool registrations into other test modules (e.g.
    # test_subprocess_utils' unknown-executable assertion).
    saved = subprocess_utils.TOOL_PATHS.copy()
    yield
    subprocess_utils.TOOL_PATHS.clear()
    subprocess_utils.TOOL_PATHS.update(saved)


def _config(*, stall_days: int = 3) -> DispatchConfig:
    return DispatchConfig(epic=EpicConfig(stall_days=stall_days))


def _advance(
    *,
    deps: FakeSource,
    facts: FakeFactsReader,
    store: Any,
    sink: FakeSink,
    lock_path: Path,
    now: datetime = NOW,
    max_parallel: int = 5,
    config: DispatchConfig | None = None,
    approvers: list[str] | None = None,
    interval_s: float | None = None,
) -> TickResult:
    return advance(
        EPIC,
        config=config or _config(),
        store=store,
        deps=deps,
        facts=facts,
        dispatch_sink=sink,
        max_parallel=max_parallel,
        now=now,
        lock_path=lock_path,
        approvers=approvers or [],
        repo=REPO,
        interval_s=interval_s,
    )


@dataclass
class DriverEnv:
    """Bundles the seams `advance()` needs for one epic under test (Task A3).

    Defaults to a single OPEN TODO node "1" — enough for the locked-out and
    preflight-raise tests, which never look past the lock/preflight gate.
    Tests that exercise the graph itself (invalid graph, normal tick) replace
    `.deps`/`.facts` before calling `.advance(...)`.
    """

    store: SqliteRunStore
    lock_path: Path
    config: DispatchConfig
    deps: FakeSource
    facts: FakeFactsReader
    sink: FakeSink

    def advance(
        self,
        *,
        now: datetime = NOW,
        max_parallel: int = 5,
        interval_s: float | None = None,
        approvers: list[str] | None = None,
    ) -> TickResult:
        return _advance(
            deps=self.deps,
            facts=self.facts,
            store=self.store,
            sink=self.sink,
            lock_path=self.lock_path,
            now=now,
            max_parallel=max_parallel,
            config=self.config,
            approvers=approvers,
            interval_s=interval_s,
        )


@pytest.fixture
def driver_env(tmp_path: Path) -> Iterator[DriverEnv]:
    """Tmp store/lock_path/config/deps/facts/sink bundle for advance() tests.

    The store file is named `dispatch.db` (not `run.db`) so a test can point
    a CLI-config `logging.dir` at the same `tmp_path` and have `kew epic
    status` open the exact same database `advance()` wrote to.
    """
    store = SqliteRunStore(str(tmp_path / "dispatch.db"))
    env = DriverEnv(
        store=store,
        lock_path=tmp_path / "epic.lock",
        config=_config(),
        deps=FakeSource(nodes=[_node("1")], edges=[]),
        facts=FakeFactsReader(issue_facts_by_num={1: _facts(1, state="OPEN")}),
        sink=FakeSink(),
    )
    yield env
    store.close()
