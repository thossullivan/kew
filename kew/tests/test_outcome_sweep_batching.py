"""The serve sweep reads PR outcomes in one batched GraphQL request a cycle and
backs off from PRs that have not moved (#2750).

The fake answers at the process boundary — ``kew.outcomes.run`` is where every
``gh`` call leaves — from a small model of a repository's pull requests, so the
tests count real requests and read real store rows.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from kew.config import DispatchConfig
from kew.outcomes import (
    _ORPHAN_COMMENT_MARKER,
    READING_TTL,
    PRReadings,
    sync_outcomes,
)
from kew.run_store import RunRecord, SqliteRunStore

T0 = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)


def _stamp(at: datetime) -> str:
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class _Repo:
    """A repository's PRs, answering ``gh api graphql`` and ``gh pr view``."""

    prs: dict[str, dict[str, Any]] = field(default_factory=dict)
    calls: list[list[str]] = field(default_factory=list)
    fail: bool = False

    def open_pr(self, branch: str, number: int, at: datetime) -> None:
        self.prs[branch] = {
            "number": number,
            "url": f"https://github.com/o/r/pull/{number}",
            "headRefName": branch,
            "state": "OPEN",
            "mergedAt": None,
            "updatedAt": _stamp(at),
            "additions": 10,
            "deletions": 2,
            "changedFiles": 1,
            "isCrossRepository": False,
            "reviews": {"totalCount": 1},
        }

    def fork_pr(self, branch: str, number: int, at: datetime) -> None:
        """An open PR from a fork whose branch has the same name as *branch*."""
        own = self.prs.pop(branch, None)
        self.open_pr(branch, number, at)
        self.prs[f"fork:{branch}"] = {**self.prs.pop(branch), "isCrossRepository": True}
        if own is not None:
            self.prs[branch] = own

    def merge(self, branch: str, at: datetime) -> None:
        pr = self.prs[branch]
        pr.update(state="MERGED", mergedAt=_stamp(at), updatedAt=_stamp(at))
        pr["reviews"] = {"totalCount": 2}

    def graphql_calls(self) -> list[list[str]]:
        return [c for c in self.calls if c[:3] == ["gh", "api", "graphql"]]

    def branches_read(self, call: list[str]) -> list[str]:
        """The branches one GraphQL request read in full."""
        values = [call[i + 1] for i, arg in enumerate(call) if arg == "-f"]
        return [v.split("=", 1)[1] for v in values if v.split("=", 1)[0][1:].isdigit()]

    def __call__(self, cmd: list[str], **_: Any) -> MagicMock:
        self.calls.append(list(cmd))
        if self.fail:
            return MagicMock(returncode=1, stdout="", stderr="HTTP 502")
        if cmd[:3] == ["gh", "pr", "view"]:
            return self._view(cmd[3])
        return self._graphql(cmd)

    def _view(self, branch: str) -> MagicMock:
        if branch.isdigit():  # the orphan pass asking for an orphan's comments
            return MagicMock(returncode=0, stdout=_ORPHAN_COMMENT_MARKER, stderr="")
        pr = self.prs[branch]
        body = {**pr, "reviews": [{}] * pr["reviews"]["totalCount"]}
        return MagicMock(returncode=0, stdout=json.dumps(body), stderr="")

    def _graphql(self, cmd: list[str]) -> MagicMock:
        query = next(a for a in cmd if a.startswith("query="))
        repository: dict[str, Any] = {}
        for i, branch in enumerate(self.branches_read(cmd)):
            found = [pr for pr in self.prs.values() if pr["headRefName"] == branch]
            repository[f"b{i}"] = {"nodes": found}
        everything = sorted(
            self.prs.values(), key=lambda pr: pr["updatedAt"], reverse=True
        )
        if " recent: " in query:
            repository["recent"] = {"nodes": everything[:50]}
        if " open: " in query:
            opened = [pr for pr in everything if pr["state"] == "OPEN"]
            repository["open"] = {"nodes": opened}
        body = {"data": {"repository": repository}}
        return MagicMock(returncode=0, stdout=json.dumps(body), stderr="")


def _config() -> DispatchConfig:
    config = DispatchConfig()
    config.project.repo = "o/r"
    return config


def _store(*branches: str) -> SqliteRunStore:
    store = SqliteRunStore(":memory:")
    for n, branch in enumerate(branches):
        _save(store, f"r{n}", branch)
    return store


def _save(store: SqliteRunStore, run_id: str, branch: str) -> None:
    store.save(
        RunRecord(
            run_id=run_id,
            issue_num=1,
            branch=branch,
            status="success",
            exit_code=0,
            started_at=T0,
        )
    )


def _sweep(
    store: SqliteRunStore, repo: _Repo, readings: PRReadings, at: datetime
) -> Any:
    with patch("kew.outcomes.run", side_effect=repo):
        return sync_outcomes(store, _config(), readings=readings, now=at)


def test_a_sweep_over_many_branches_makes_one_graphql_call() -> None:
    branches = [f"agent/issue-{n}" for n in range(7)]
    store = _store(*branches)
    repo = _Repo()
    for n, branch in enumerate(branches):
        repo.open_pr(branch, 100 + n, T0)

    result = _sweep(store, repo, PRReadings(), T0)

    assert len(repo.calls) == 1
    assert repo.graphql_calls() == repo.calls
    assert sorted(repo.branches_read(repo.calls[0])) == sorted(branches)
    assert result.synced == 7 and result.query_failures == 0
    assert result.graphql_calls == 1
    got = store.get("r3")
    assert got is not None
    assert got.pr_state == "OPEN" and got.pr_url.endswith("/pull/103")
    assert got.review_cycles == 1 and got.diff_additions == 10


def test_more_branches_than_one_batch_holds_split_into_batches() -> None:
    branches = [f"agent/issue-{n}" for n in range(51)]
    store = _store(*branches)
    repo = _Repo()
    for n, branch in enumerate(branches):
        repo.open_pr(branch, 100 + n, T0)

    result = _sweep(store, repo, PRReadings(), T0)

    assert len(repo.graphql_calls()) == 2
    assert result.synced == 51 and result.graphql_calls == 2


def test_a_branch_with_no_pr_reads_absent() -> None:
    store = _store("agent/issue-1")
    repo = _Repo()

    result = _sweep(store, repo, PRReadings(), T0)

    assert result.no_pr == 1
    got = store.get("r0")
    assert got is not None and got.pr_state == "ABSENT"


def test_an_unchanged_open_pr_read_minutes_ago_is_not_read_again() -> None:
    store = _store("agent/issue-1")
    repo = _Repo()
    repo.open_pr("agent/issue-1", 7, T0 - timedelta(hours=1))
    readings = PRReadings()
    _sweep(store, repo, readings, T0)

    _sweep(store, repo, readings, T0 + timedelta(minutes=1))
    _sweep(store, repo, readings, T0 + timedelta(minutes=4))

    later = repo.graphql_calls()[1:]
    assert len(later) == 2  # still one request a cycle: the change list
    assert all(repo.branches_read(call) == [] for call in later)

    _sweep(store, repo, readings, T0 + READING_TTL)
    assert repo.branches_read(repo.calls[-1]) == ["agent/issue-1"]


def test_the_run_id_path_reads_at_once_inside_the_back_off() -> None:
    store = _store("agent/issue-1")
    repo = _Repo()
    repo.open_pr("agent/issue-1", 7, T0 - timedelta(hours=1))
    readings = PRReadings()
    _sweep(store, repo, readings, T0)
    repo.merge("agent/issue-1", T0 + timedelta(seconds=30))

    with patch("kew.outcomes.run", side_effect=repo):
        result = sync_outcomes(store, _config(), run_id="r0", readings=readings, now=T0)

    assert repo.calls[-1][:3] == ["gh", "pr", "view"]
    assert result.synced == 1
    got = store.get("r0")
    assert got is not None and got.pr_state == "MERGED"


def test_a_merge_settles_pr_state_in_the_next_cycle() -> None:
    store = _store("agent/issue-1")
    repo = _Repo()
    repo.open_pr("agent/issue-1", 7, T0 - timedelta(hours=1))
    readings = PRReadings()
    _sweep(store, repo, readings, T0)
    repo.merge("agent/issue-1", T0 + timedelta(seconds=30))

    result = _sweep(store, repo, readings, T0 + timedelta(minutes=1))

    assert result.synced == 1 and result.graphql_calls == 1
    got = store.get("r0")
    assert got is not None
    assert got.pr_state == "MERGED"
    assert got.pr_merged_at == _stamp(T0 + timedelta(seconds=30))
    assert got.review_cycles == 2


def test_a_fork_pr_from_a_same_named_branch_is_not_the_runs_pr() -> None:
    store = _store("agent/issue-1")
    repo = _Repo()
    repo.open_pr("agent/issue-1", 7, T0 - timedelta(hours=1))
    repo.merge("agent/issue-1", T0 - timedelta(minutes=30))
    repo.fork_pr("agent/issue-1", 66, T0)

    _sweep(store, repo, PRReadings(), T0)

    got = store.get("r0")
    assert got is not None
    assert got.pr_state == "MERGED" and got.pr_url.endswith("/pull/7")


def test_a_fork_pr_alone_on_a_branch_reads_absent() -> None:
    store = _store("agent/issue-1")
    repo = _Repo()
    repo.fork_pr("agent/issue-1", 66, T0)

    result = _sweep(store, repo, PRReadings(), T0)

    assert result.no_pr == 1
    got = store.get("r0")
    assert got is not None and got.pr_state == "ABSENT"


def test_a_fork_pr_moving_does_not_unsettle_a_resting_branch() -> None:
    store = _store("agent/issue-1")
    repo = _Repo()
    repo.open_pr("agent/issue-1", 7, T0 - timedelta(hours=1))
    readings = PRReadings()
    _sweep(store, repo, readings, T0)
    repo.fork_pr("agent/issue-1", 66, T0 + timedelta(seconds=30))
    repo.merge("agent/issue-1", T0 + timedelta(seconds=40))

    _sweep(store, repo, readings, T0 + timedelta(minutes=1))

    got = store.get("r0")
    assert got is not None
    assert got.pr_state == "MERGED" and got.pr_url.endswith("/pull/7")


def test_a_close_settles_pr_state_in_the_next_cycle() -> None:
    store = _store("agent/issue-1")
    repo = _Repo()
    repo.open_pr("agent/issue-1", 7, T0 - timedelta(hours=1))
    readings = PRReadings()
    _sweep(store, repo, readings, T0)
    repo.prs["agent/issue-1"].update(state="CLOSED", updatedAt=_stamp(T0))

    _sweep(store, repo, readings, T0 + timedelta(minutes=1))

    got = store.get("r0")
    assert got is not None and got.pr_state == "CLOSED"


def test_a_new_run_on_a_resting_branch_is_read_at_once() -> None:
    store = _store("agent/issue-1")
    repo = _Repo()
    repo.open_pr("agent/issue-1", 7, T0 - timedelta(hours=1))
    readings = PRReadings()
    _sweep(store, repo, readings, T0)

    _save(store, "r-new", "agent/issue-1")
    _sweep(store, repo, readings, T0 + timedelta(minutes=1))

    assert repo.branches_read(repo.calls[-1]) == ["agent/issue-1"]
    got = store.get("r-new")
    assert got is not None and got.pr_state == "OPEN"


def test_a_touched_branch_is_read_on_the_next_sweep() -> None:
    store = _store("agent/issue-1")
    repo = _Repo()
    repo.open_pr("agent/issue-1", 7, T0 - timedelta(hours=1))
    readings = PRReadings()
    _sweep(store, repo, readings, T0)

    readings.touch("agent/issue-1")
    _sweep(store, repo, readings, T0 + timedelta(minutes=1))

    assert repo.branches_read(repo.calls[-1]) == ["agent/issue-1"]


def test_a_change_list_that_may_have_skipped_a_pr_forgets_every_reading() -> None:
    """Fifty updates since the last cycle fill the list; a PR updated before
    them could be missing from it, so every resting branch is read again."""
    store = _store("agent/issue-1")
    repo = _Repo()
    repo.open_pr("agent/issue-1", 7, T0 - timedelta(hours=1))
    readings = PRReadings()
    _sweep(store, repo, readings, T0)

    for n in range(50):
        repo.open_pr(f"human/{n}", 500 + n, T0 + timedelta(seconds=n + 1))
    _sweep(store, repo, readings, T0 + timedelta(minutes=1))
    assert repo.branches_read(repo.calls[-1]) == []

    _sweep(store, repo, readings, T0 + timedelta(minutes=2))
    assert repo.branches_read(repo.calls[-1]) == ["agent/issue-1"]


def test_fork_prs_on_a_full_change_list_still_count_towards_its_overflow() -> None:
    """A fork's PR fills a slot on the recently-updated page like any other;
    dropping it before the page is measured hid an own merge pushed off it."""
    store = _store("agent/issue-1")
    repo = _Repo()
    repo.open_pr("agent/issue-1", 7, T0 - timedelta(hours=1))
    readings = PRReadings()
    _sweep(store, repo, readings, T0)

    repo.merge("agent/issue-1", T0 + timedelta(seconds=1))
    for n in range(49):
        repo.open_pr(f"human/{n}", 500 + n, T0 + timedelta(seconds=n + 2))
    repo.fork_pr("contrib/x", 600, T0 + timedelta(seconds=51))
    _sweep(store, repo, readings, T0 + timedelta(minutes=1))
    _sweep(store, repo, readings, T0 + timedelta(minutes=2))

    got = store.get("r0")
    assert got is not None and got.pr_state == "MERGED"


def test_a_failed_request_leaves_outcomes_and_counts_each_branch() -> None:
    store = _store("agent/issue-1", "agent/issue-2")
    repo = _Repo(fail=True)

    result = _sweep(store, repo, PRReadings(), T0)

    assert result.query_failures == 2 and result.graphql_calls == 1
    got = store.get("r0")
    assert got is not None and got.pr_state is None


def test_nothing_eligible_makes_no_request() -> None:
    repo = _Repo()

    result = _sweep(SqliteRunStore(":memory:"), repo, PRReadings(), T0)

    assert repo.calls == [] and result.graphql_calls == 0


def test_the_serve_cycle_makes_one_graphql_call_and_reports_it(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """The orphan pass reads the open PRs the batched request already listed."""
    from kew.cli import serve as serve_cli
    from kew.github_rate_limit import RateLimitBucket, RateLimitReading

    db = tmp_path / "dispatch.db"
    with SqliteRunStore(str(db)) as store:
        _save(store, "r0", "agent/issue-1")
    repo = _Repo()
    repo.open_pr("agent/issue-1", 7, T0)
    repo.open_pr("agent/issue-9", 9, T0)  # no run behind it: an orphan
    reading = RateLimitReading(
        observed_at="2026-10-11T12:00:00+00:00",
        core=RateLimitBucket(limit=5000, remaining=4987, reset_at=None),
        ok=True,
        status=200,
    )
    monkeypatch.setattr("kew.github_rate_limit.read_rate_limit", lambda **k: reading)
    config = _config()

    with patch("kew.outcomes.run", side_effect=repo):
        result = serve_cli.sweep_cycle(
            lambda: SqliteRunStore(str(db)), config, readings=PRReadings()
        )

    assert len(repo.graphql_calls()) == 1
    assert ["gh", "pr", "list"] not in [c[:3] for c in repo.calls]
    assert result.graphql_calls == 1 and result.synced == 1
    with SqliteRunStore(str(db)) as store:
        assert [o.pr_number for o in store.list_orphan_prs()] == [9]
