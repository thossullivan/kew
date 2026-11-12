"""A node whose PR closed unmerged reads the same everywhere (#2689).

Its overlay status is TODO, so `kew epic status` listed it ready while every
tick's dispatch was refused, silently, by the closed PR's branch still on the
remote. Both now report it stuck `closed-unmerged`, naming the PR, and the
operator's restart (`kew epic retry`) lets the next tick dispatch it afresh.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from support import driver_fakes as _fx
from support.driver_fakes import (
    EPIC,
    NOW,
    REPO,
    FakeFactsReader,
    FakeSink,
    FakeSource,
    _advance,
    _config,
    _facts,
    _node,
)
from support.pr_batch import is_pr_batch, pr_batch_result
from typer.testing import CliRunner

from kew.cli import epic_app
from kew.cli.epic import _print_tick_result
from kew.config import DispatchConfig
from kew.graph.build import build_graph
from kew.graph.driver.facts import GitHubFactsReader
from kew.graph.driver.tick import summarize
from kew.run_store import RunRecord, SqliteRunStore
from kew.service.verbs import epic_retry

# Re-exported by assignment (not `from x import y`) so ruff's F811 doesn't
# flag the fixture parameter that shadows it below.
store = _fx.store
_no_real_git = _fx._no_real_git
_isolate_tool_paths = _fx._isolate_tool_paths

REASON = "closed-unmerged: PR #77 closed without merging"
RESTART = f"restart with `kew epic retry {EPIC} 1`"


def _pr(number: int, state: str) -> dict[str, Any]:
    return {
        "number": number,
        "url": f"https://github.com/o/r/pull/{number}",
        "state": state,
        "headRefName": "agent/issue-1",
        "reviewDecision": None,
        "mergeStateStatus": "CLEAN",
        "headRefOid": f"head{number}",
        "commits": [],
        "latestReviews": [],
        "author": {"login": "agent"},
    }


def _gh(prs: list[dict[str, Any]]) -> Any:
    """A `gh` fake: issue 1 open with no closing reference, these PRs on its branch."""
    issue = {
        "number": 1,
        "state": "OPEN",
        "stateReason": None,
        "labels": [],
        "closedByPullRequestsReferences": [],
    }
    by_number = {pr["number"]: pr for pr in prs}

    def run(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        payload: Any
        if command[:3] == ["gh", "issue", "view"]:
            payload = issue
        elif is_pr_batch(command):
            return pr_batch_result(command, prs)
        elif command[:3] == ["gh", "pr", "list"]:
            payload = prs
        elif command[:3] == ["gh", "pr", "view"]:
            payload = by_number[int(command[3])]
        elif command[:3] == ["gh", "issue", "edit"]:
            payload = {}
        else:
            raise AssertionError(f"unexpected command: {command}")
        return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

    return run


def _published_run(run_id: str, pr_number: int, started_at: datetime) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        issue_num=1,
        item_id="1",
        branch="agent/issue-1",
        status="success",
        exit_code=0,
        started_at=started_at,
        finished_at=started_at,
        pr_url=f"https://github.com/o/r/pull/{pr_number}",
    )


def _status_cli(tmp_path: Path, facts: Any, *args: str) -> Any:
    cfg = DispatchConfig()
    cfg.project.repo = REPO
    cfg.logging.dir = str(tmp_path)
    graph = build_graph(FakeSource(nodes=[_node("1")], edges=[]), EPIC)
    with (
        patch("kew.cli._load_epic_graph", return_value=graph),
        patch("kew.cli.load_config", return_value=cfg),
        patch("kew.cli.preflight_tools"),
        patch("kew.cli.GitHubFactsReader", return_value=facts),
    ):
        invoked = CliRunner().invoke(epic_app, ["status", EPIC, *args])
    assert invoked.exit_code == 0, invoked.output
    return invoked


def test_status_and_tick_report_one_closed_unmerged_reason(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # `kew epic status` opens logging.dir/dispatch.db: the store this test writes.
    store = SqliteRunStore(str(tmp_path / "dispatch.db"))
    store.save(_published_run("implementation", 77, NOW))
    facts = GitHubFactsReader(
        "o/r", store=store, host="github.com", run=_gh([_pr(77, "CLOSED")])
    )
    graph = build_graph(FakeSource(nodes=[_node("1")], edges=[]), EPIC)

    summary = summarize(EPIC, graph, config=_config(), store=store, facts=facts)
    assert summary.frontier == ()
    assert [(s.node_id, s.reason) for s in summary.stuck] == [("1", REASON)]

    sink = FakeSink()
    result = _advance(
        deps=FakeSource(nodes=[_node("1")], edges=[]),
        facts=facts,
        store=store,
        sink=sink,
        lock_path=tmp_path / "epic.lock",
    )
    assert sink.calls == []
    assert [(s.node_id, s.reason) for s in result.stuck] == [("1", REASON)]

    _print_tick_result(EPIC, result, _config())
    tick_out = capsys.readouterr()
    tick_text = tick_out.out + tick_out.err
    assert f"stuck: #1 ({REASON})" in tick_text
    assert RESTART in tick_text

    text = _status_cli(tmp_path, facts)
    # The text view itself (stdout) names it under `Stuck:` (#2695).
    assert f"Stuck:\n  #1 {REASON} — {RESTART}" in text.stdout
    assert "Ready to dispatch: ['1']" not in text.output

    doc = json.loads(_status_cli(tmp_path, facts, "--json").stdout)
    assert doc["frontier"] == []
    assert [(s["id"], s["reason"]) for s in doc["stuck"]] == [("1", REASON)]
    nodes = {n["id"]: n for n in doc["nodes"]}
    assert nodes["1"]["stuck"] == REASON
    store.close()


def test_retry_restarts_the_node_and_its_new_pr_becomes_the_link(
    tmp_path: Path, store: Any
) -> None:
    store.save(_published_run("implementation", 77, NOW))
    config = _config()
    config.project.repo = REPO
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(
            "kew.service.verbs.mark_ready_from_failed", lambda *a, **k: None
        )
        epic_retry(store, epic_ref=EPIC, item_id="1", config=config)

    facts = GitHubFactsReader(
        "o/r", store=store, host="github.com", run=_gh([_pr(77, "CLOSED")])
    )
    sink = FakeSink()
    result = _advance(
        deps=FakeSource(nodes=[_node("1")], edges=[]),
        facts=facts,
        store=store,
        sink=sink,
        lock_path=tmp_path / "epic.lock",
        config=config,
    )
    assert result.dispatched == ("1",)
    assert sink.calls == ["1"]
    assert result.stuck == ()

    # The restarted run publishes PR #78; the closed #77 stays in the ledger.
    reservation = next(iter(store.list_epic_reservations(EPIC)))
    store.save(_published_run(reservation.run_id, 78, datetime.now(NOW.tzinfo)))
    fresh = GitHubFactsReader(
        "o/r",
        store=store,
        host="github.com",
        run=_gh([_pr(77, "CLOSED"), _pr(78, "OPEN")]),
    )
    linked = fresh.issue_facts(1)
    assert (linked.pr_number, linked.pr_state, linked.pr_link_source) == (
        78,
        "OPEN",
        "ledger",
    )
    assert store.get("implementation").pr_url.endswith("/pull/77")


def test_a_retry_older_than_the_closed_prs_run_does_not_release_it(
    tmp_path: Path, store: Any
) -> None:
    """The marker only restarts the run it postdates: a retry recorded before
    the run that opened the closed PR leaves the node stuck."""
    store.mark_epic_retry(EPIC, 1)
    later = datetime.now(NOW.tzinfo).replace(year=2099)
    store.save(_published_run("implementation", 77, later))
    facts = FakeFactsReader(
        issue_facts_by_num={1: _facts(1, pr_number=77, pr_state="CLOSED")}
    )
    sink = FakeSink()
    result = _advance(
        deps=FakeSource(nodes=[_node("1")], edges=[]),
        facts=facts,
        store=store,
        sink=sink,
        lock_path=tmp_path / "epic.lock",
    )
    assert sink.calls == []
    assert [(s.node_id, s.reason) for s in result.stuck] == [("1", REASON)]


def test_a_leftover_remote_branch_is_reported_not_skipped(
    tmp_path: Path, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A restarted node whose closed PR's branch is still on the remote: the
    dispatch is refused, and the tick says so instead of skipping it quietly."""
    monkeypatch.setattr(
        "kew.graph.driver.tick.remote_branch_exists", lambda branch: True
    )
    sink = FakeSink()
    result = _advance(
        deps=FakeSource(nodes=[_node("1")], edges=[]),
        facts=FakeFactsReader(issue_facts_by_num={1: _facts(1)}),
        store=store,
        sink=sink,
        lock_path=tmp_path / "epic.lock",
    )
    assert sink.calls == []
    assert store.list_epic_reservations(EPIC) == []
    [stuck] = result.stuck
    assert stuck.node_id == "1"
    assert stuck.reason.startswith("branch-exists: remote branch 'agent/issue-1'")
    assert "git push origin --delete agent/issue-1" in stuck.reason
