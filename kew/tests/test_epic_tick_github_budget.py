"""#2582 — one epic tick's GitHub budget.

Three epic loops and the console spent ~5,300 GraphQL points an hour against
GitHub's 5,000: every tick read every node's PRs one ``gh`` call at a time,
whether or not anything could have changed. These tests drive a real tick — the
GitHub-native graph read and the facts reader both — through one fake ``gh``
that counts what it is asked, so the budget is a number a test can hold.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from support.pr_batch import (
    batch_variables,
    batched_branches,
    batched_numbers,
    is_pr_batch,
    pr_batch_result,
)

from kew.cli.epic import _points_suffix
from kew.config import DispatchConfig, EpicConfig
from kew.graph.adapters.github_native import GitHubNativeSource
from kew.graph.driver.facts import GitHubFactsReader
from kew.graph.driver.tick import advance
from kew.run_store import RunRecord, SqliteRunStore

REPO = "o/r"
PARENT = "7"
DONE = range(1, 11)
NOT_STARTED = range(11, 21)
IN_PROGRESS = range(21, 26)
IN_REVIEW = range(26, 31)


def _pr_url(number: int) -> str:
    return f"https://github.com/{REPO}/pull/{number}"


def _sub_issue(number: int) -> dict[str, Any]:
    """One sub-issue as the native adapter's GraphQL query returns it."""
    closing: list[dict[str, Any]] = []
    if number in DONE:
        closing = [{"number": 100 + number, "url": _pr_url(100 + number)}]
        closing[0]["state"] = "MERGED"
    elif number in IN_REVIEW:
        closing = [{"number": 100 + number, "url": _pr_url(100 + number)}]
        closing[0]["state"] = "OPEN"
    return {
        "number": number,
        "title": f"node {number}",
        "url": f"https://github.com/{REPO}/issues/{number}",
        "state": "CLOSED" if number in DONE else "OPEN",
        "stateReason": "COMPLETED" if number in DONE else None,
        "body": "",
        "labels": {"nodes": []},
        "repository": {"nameWithOwner": REPO},
        "blockedBy": {"nodes": []},
        "closedByPullRequestsReferences": {"nodes": closing},
    }


def _open_pr(number: int) -> dict[str, Any]:
    return {
        "number": 100 + number,
        "url": _pr_url(100 + number),
        "state": "OPEN",
        "headRefName": f"agent/issue-{number}",
        "reviewDecision": None,
        "mergeStateStatus": "BLOCKED",
        "headRefOid": f"head{number}",
        "commits": [],
        "latestReviews": [],
        "author": {"login": "agent"},
    }


class CountingGh:
    """Every ``gh`` invocation the tick makes, answered from one fixture epic."""

    def __init__(self, sub_issues: list[dict[str, Any]], prs: list[dict[str, Any]]):
        self.calls: list[list[str]] = []
        self._sub_issues = sub_issues
        self._prs = prs

    @property
    def batches(self) -> list[list[str]]:
        return [call for call in self.calls if is_pr_batch(call)]

    def __call__(self, cmd: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(cmd)
        if is_pr_batch(cmd):
            return pr_batch_result(cmd, self._prs, cost=1)
        query = next((a[6:] for a in cmd if a.startswith("query=")), "")
        if cmd[:3] == ["gh", "api", "graphql"] and "subIssues(first: 100" in query:
            payload = {
                "data": {
                    "rateLimit": {"cost": 1},
                    "repository": {
                        "issue": {
                            "subIssues": {
                                "nodes": self._sub_issues,
                                "pageInfo": {"hasNextPage": False},
                            }
                        }
                    },
                }
            }
            return subprocess.CompletedProcess(cmd, 0, json.dumps(payload), "")
        # Anything else is a call the budget did not plan for: answer it as a
        # failure so the tick carries on and the count below names it.
        return subprocess.CompletedProcess(cmd, 1, "", "unscripted")


def _seed_store(store: SqliteRunStore) -> None:
    now = datetime.now(UTC)
    for number in (*DONE, *IN_PROGRESS, *IN_REVIEW):
        store.save(
            RunRecord(
                run_id=f"run-{number}",
                issue_num=number,
                item_id=str(number),
                branch=f"agent/issue-{number}",
                status="pending" if number in IN_PROGRESS else "success",
                exit_code=0,
                started_at=now,
                finished_at=None if number in IN_PROGRESS else now,
                pr_url=None if number in IN_PROGRESS else _pr_url(100 + number),
            )
        )


@pytest.fixture(autouse=True)
def _no_real_git(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "kew.graph.driver.tick.remote_branch_exists", lambda branch: False
    )


def _tick(tmp_path: Path, gh: CountingGh) -> Any:
    with SqliteRunStore(str(tmp_path / "runs.db")) as store:
        _seed_store(store)
        result = advance(
            PARENT,
            config=DispatchConfig(epic=EpicConfig()),
            store=store,
            deps=GitHubNativeSource(REPO, run=gh),
            facts=GitHubFactsReader(REPO, store=store, run=gh, host="github.com"),
            dispatch_sink=_NoDispatch(),
            max_parallel=0,
            now=datetime.now(UTC),
            lock_path=tmp_path / "epic.lock",
            approvers=[],
            repo=REPO,
        )
    return result


class _NoDispatch:
    def dispatch(self, item_id: str) -> bool:
        raise AssertionError(f"max_parallel=0 dispatched {item_id}")


def test_a_thirty_node_tick_makes_at_most_three_github_calls(tmp_path: Path) -> None:
    gh = CountingGh(
        [_sub_issue(n) for n in range(1, 31)], [_open_pr(n) for n in IN_REVIEW]
    )

    result = _tick(tmp_path, gh)

    gh_calls = [call for call in gh.calls if call[0] == "gh"]
    assert len(gh_calls) <= 3, gh_calls
    # The graph read and the one PR batch — not one read per node.
    assert len(gh.batches) == 1
    assert set(result.done) == {str(n) for n in DONE}
    assert {node.node_id for node in result.waiting} >= {str(n) for n in IN_REVIEW}
    # Both queries report their cost, and the tick adds them up.
    assert result.graphql_points == 2
    assert _points_suffix(result) == " graphql_points=2"


def test_done_and_not_started_nodes_trigger_no_pr_lookup(tmp_path: Path) -> None:
    gh = CountingGh(
        [_sub_issue(n) for n in range(1, 31)], [_open_pr(n) for n in IN_REVIEW]
    )

    _tick(tmp_path, gh)

    (batch,) = gh.batches
    asked_branches = set(batched_branches(batch))
    asked_numbers = set(batched_numbers(batch))
    live = (*IN_PROGRESS, *IN_REVIEW)
    assert asked_branches == {f"agent/issue-{n}" for n in live}
    assert asked_numbers == {100 + n for n in IN_REVIEW}
    for number in (*DONE, *NOT_STARTED):
        assert f"agent/issue-{number}" not in asked_branches
        assert 100 + number not in asked_numbers
    # A never-started node is asked only whether its branch exists.
    probes = {
        value.removeprefix("refs/heads/")
        for key, value in batch_variables(batch).items()
        if key.startswith("r")
    }
    assert probes == {f"agent/issue-{n}" for n in NOT_STARTED}
    # And no node paid a per-node read on top.
    assert not [c for c in gh.calls if c[1:3] in (["pr", "list"], ["pr", "view"])]
    assert not [c for c in gh.calls if c[1:3] == ["issue", "view"]]


def test_a_not_started_node_whose_branch_exists_still_links_its_pr(
    tmp_path: Path,
) -> None:
    """Work begun outside this store — another machine, a reset ledger — has no
    run row, but its branch exists; the probe finds it and a second query reads
    its PR, so the node reads in review rather than dispatchable."""
    elsewhere = _open_pr(11)
    gh = CountingGh(
        [_sub_issue(n) for n in range(1, 31)],
        [*(_open_pr(n) for n in IN_REVIEW), elsewhere],
    )

    result = _tick(tmp_path, gh)

    assert len(gh.batches) == 2
    assert batched_branches(gh.batches[1]) == ["agent/issue-11"]
    assert "11" in {node.node_id for node in result.waiting}
