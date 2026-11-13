import json
import subprocess
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import typer
from typer.testing import CliRunner

import kew.cli as cli
from kew.cli import epic_app
from kew.config import DispatchConfig
from kew.graph.adapters.github_convention import GitHubConventionSource
from kew.graph.build import build_graph
from kew.graph.domain import Edge, Graph, Node, NodeStatus

runner = CliRunner()


def _graph_with_cycle():
    n = [
        Node(id=str(i), title=f"t{i}", status=NodeStatus.TODO, epic_ref="foo")
        for i in (1, 2)
    ]
    return Graph(n, [Edge("1", "2"), Edge("2", "1")])


def _clean_graph():
    n = [
        Node(id=str(i), title=f"t{i}", status=NodeStatus.TODO, epic_ref="foo")
        for i in (1, 2)
    ]
    return Graph(n, [Edge("1", "2")])


def test_graph_command_emits_mermaid():
    with patch("kew.cli._load_epic_graph", return_value=_clean_graph()):
        result = runner.invoke(epic_app, ["graph", "foo", "--format", "mermaid"])
    assert result.exit_code == 0
    assert "graph TD" in result.stdout


def test_validate_fails_on_cycle():
    with patch("kew.cli._load_epic_graph", return_value=_graph_with_cycle()):
        result = runner.invoke(epic_app, ["validate", "foo"])
    assert result.exit_code == 1
    assert "cycle" in result.stdout.lower()


def test_validate_passes_clean():
    with patch("kew.cli._load_epic_graph", return_value=_clean_graph()):
        result = runner.invoke(epic_app, ["validate", "foo"])
    assert result.exit_code == 0


def test_validate_warns_on_a_two_comment_body_and_still_passes():
    graph = _clean_graph()
    graph.multi_depends = ["2"]
    with patch("kew.cli._load_epic_graph", return_value=graph):
        result = runner.invoke(epic_app, ["validate", "foo"])
    assert result.exit_code == 0
    assert "Warning: #2 carries more than one kew:depends comment" in result.stdout
    assert "OK: 2 nodes" in result.stdout


def _issue(number: int, body: str, state: str = "OPEN") -> dict[str, Any]:
    return {
        "number": number,
        "title": f"t{number}",
        "url": f"https://github.com/o/r/issues/{number}",
        "state": state,
        "stateReason": None,
        "labels": [{"name": "epic:foo"}],
        "body": body,
    }


def _graph_from_bodies(bodies: dict[int, str], outside: tuple[int, ...] = ()):
    """Build the epic graph the way `kew epic validate` does, faking only gh."""
    members = [_issue(n, body) for n, body in bodies.items()]

    def run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if cmd[:3] == ["gh", "issue", "list"]:
            return subprocess.CompletedProcess(cmd, 0, json.dumps(members), "")
        number = int(cmd[3])
        if number in outside:
            closed = _issue(number, "", state="CLOSED")
            return subprocess.CompletedProcess(cmd, 0, json.dumps(closed), "")
        return subprocess.CompletedProcess(cmd, 1, "", "not found")

    return build_graph(GitHubConventionSource(repo="o/r", run_fn=run), "foo")


def test_validate_reports_a_cycle_written_with_hash_and_bare_tokens():
    graph = _graph_from_bodies(
        {1: "<!-- kew:depends #2 -->", 2: "<!-- kew:depends 1 -->"}, outside=(1, 2)
    )
    with patch("kew.cli._load_epic_graph", return_value=graph):
        result = runner.invoke(epic_app, ["validate", "foo"])
    assert result.exit_code == 1
    assert "Cycle detected" in result.stdout


def test_validate_counts_members_and_names_external_nodes_apart():
    graph = _graph_from_bodies(
        {1: "", 2: "<!-- kew:depends #1 #900 -->"}, outside=(900,)
    )
    with patch("kew.cli._load_epic_graph", return_value=graph):
        result = runner.invoke(epic_app, ["validate", "foo"])
    assert result.exit_code == 0
    assert "OK: 2 nodes, 2 dependency levels." in result.stdout
    assert "External dependencies (outside the epic): ['900']" in result.stdout


def test_validate_prints_no_warning_for_single_comment_bodies():
    with patch("kew.cli._load_epic_graph", return_value=_clean_graph()):
        result = runner.invoke(epic_app, ["validate", "foo"])
    assert "Warning" not in result.stdout


def test_empty_epic_errors(tmp_path):
    # `epic status` builds the graph inside the run-store block now (W2/#894),
    # so preflight + config resolve first; mirror the sibling status tests by
    # stubbing them, leaving the empty-epic SystemExit(2) as the exit under test.
    cfg = DispatchConfig()
    cfg.project.repo = "owner/repo"
    cfg.logging.dir = str(tmp_path)
    with (
        patch("kew.cli.load_config", return_value=cfg),
        patch("kew.cli.preflight_tools"),
        patch("kew.cli._load_epic_graph", side_effect=SystemExit(2)),
    ):
        result = runner.invoke(epic_app, ["status", "foo"])
    assert result.exit_code == 2


def test_graph_invalid_format_errors_cleanly():
    with patch("kew.cli._load_epic_graph", return_value=_clean_graph()):
        result = runner.invoke(epic_app, ["graph", "foo", "--format", "svg"])
    assert result.exit_code == 2
    assert result.exception is None or isinstance(result.exception, SystemExit)


def _graph_with_dangling_dep():
    # Node "1" is TODO and depends on "999", which is not a known node (dangling).
    n = [Node(id="1", title="t1", status=NodeStatus.TODO, epic_ref="foo")]
    return Graph(n, [Edge("999", "1")])


def test_status_excludes_node_with_dangling_dependency(tmp_path):
    cfg = DispatchConfig()
    cfg.project.repo = "owner/repo"
    cfg.logging.dir = str(tmp_path)
    with (
        patch("kew.cli._load_epic_graph", return_value=_graph_with_dangling_dep()),
        patch("kew.cli.load_config", return_value=cfg),
    ):
        result = runner.invoke(epic_app, ["status", "foo"])
    assert result.exit_code == 0
    # A dangling dependency makes the whole graph invalid, so `summarize`
    # reports it via the all-nodes-stuck path (see `_invalid_summary`) — the
    # frontier stays empty either way.
    assert "Ready to dispatch: []" in result.stdout


def test_status_names_the_epic_holding_the_poller_lock(tmp_path):
    """`kew epic status` surfaces who holds THIS epic's per-epic driver lock —
    pid, epic and held-since — so a locked-out operator sees who's driving
    (#861/F2)."""
    from kew.cli import _epic_lock_path
    from kew.graph.driver.lock import single_driver_lock

    cfg = DispatchConfig()
    cfg.project.repo = "owner/repo"
    cfg.logging.dir = str(tmp_path)
    with (
        patch("kew.cli._load_epic_graph", return_value=_graph_with_dangling_dep()),
        patch("kew.cli.load_config", return_value=cfg),
        patch("kew.cli.preflight_tools"),
        single_driver_lock(_epic_lock_path(cfg, "foo"), holder="foo") as got,
    ):
        assert got is True
        result = runner.invoke(epic_app, ["status", "foo"])
    assert result.exit_code == 0
    assert "Poller:" in result.stdout
    assert "epic=foo" in result.stdout
    assert "holds epic foo's driver lock" in result.stdout


def test_status_omits_poller_line_when_lock_is_free(tmp_path):
    cfg = DispatchConfig()
    cfg.project.repo = "owner/repo"
    cfg.logging.dir = str(tmp_path)
    with (
        patch("kew.cli._load_epic_graph", return_value=_graph_with_dangling_dep()),
        patch("kew.cli.load_config", return_value=cfg),
        patch("kew.cli.preflight_tools"),
    ):
        result = runner.invoke(epic_app, ["status", "foo"])
    assert result.exit_code == 0
    assert "Poller:" not in result.stdout


def test_load_epic_graph_registers_gh_in_allowlist():
    """Regression: `_load_epic_graph` must call `preflight_tools()` so `gh` is
    on the subprocess allowlist before the GitHubConventionSource runs
    `gh issue list`. Without it, every `kew epic` command died with
    "Unknown executable 'gh' — not in allowlist" against a real repo. The other
    tests here mock `_load_epic_graph` away, so this exercises it directly."""
    cfg = MagicMock()
    cfg.project.repo = "owner/repo"
    cfg.tracker.type = "github"  # preflight is the GitHub path's requirement
    with (
        patch("kew.cli.preflight_tools") as preflight,
        patch("kew.cli.load_config", return_value=cfg),
        patch("kew.cli._detect_repo"),
        patch("kew.cli.build_graph", return_value=Graph([], [])),
        pytest.raises(typer.Exit) as exc,
    ):
        cli._load_epic_graph("nonexistent-epic")
    preflight.assert_called_once()
    assert exc.value.exit_code == 2  # empty-epic guard reached, no allowlist crash


def test_status_reads_the_driver_liveness_from_the_lock_file(tmp_path):
    """`kew epic status` reports a liveness READING, not just who holds the lock
    (#2297): the lock file's live pid and fresh heartbeat are the signal when no
    tick has finished yet."""
    from kew.cli import _epic_lock_path
    from kew.graph.driver.lock import single_driver_lock

    cfg = DispatchConfig()
    cfg.project.repo = "owner/repo"
    cfg.logging.dir = str(tmp_path)
    with (
        patch("kew.cli._load_epic_graph", return_value=_graph_with_dangling_dep()),
        patch("kew.cli.load_config", return_value=cfg),
        patch("kew.cli.preflight_tools"),
        single_driver_lock(_epic_lock_path(cfg, "foo"), holder="foo") as got,
    ):
        assert got is True
        result = runner.invoke(epic_app, ["status", "foo"])
    assert result.exit_code == 0
    assert "Driver: live" in result.stdout
    assert "(expected)" in result.stdout  # the cadence came from the config


def test_status_says_the_driver_is_gone_when_the_holder_pid_is_dead(tmp_path):
    from kew.graph.driver.lock import LockHolder

    cfg = DispatchConfig()
    cfg.project.repo = "owner/repo"
    cfg.logging.dir = str(tmp_path)
    now = datetime.now(UTC)
    # A pid no process owns, with a heartbeat refreshed a second ago.
    holder = LockHolder(epic="foo", pid=2**22, held_since=now, heartbeat=now)
    assert holder.is_alive() is False
    with (
        patch("kew.cli._load_epic_graph", return_value=_graph_with_dangling_dep()),
        patch("kew.cli.load_config", return_value=cfg),
        patch("kew.cli.preflight_tools"),
        patch("kew.cli.lock_holder", return_value=holder),
    ):
        result = runner.invoke(epic_app, ["status", "foo"])
    assert result.exit_code == 0
    # A dead holder pid is conclusive even with a fresh heartbeat (#970's shape).
    # The gone reading is a warning, so it lands on stderr with the other ones.
    assert "Driver: gone" in result.output
