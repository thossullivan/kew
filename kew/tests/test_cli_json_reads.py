"""`--json` on the read commands agents poll (#2504).

`kew epic status`, `kew setup`, `kew skills list` and `kew triage queue` each
print ONE JSON document on stdout carrying a `schema` version, built from the
same data the text output reads. Progress lines stay on stderr, so stdout
parses whole.
"""

from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from kew.cli import _open_run_store, app
from kew.config import DispatchConfig
from kew.graph.domain import Edge, Graph, Node, NodeStatus
from kew.graph.driver.result import StuckNode, TerminalSignal, TickSummary
from kew.run_store import SqliteRunStore

if TYPE_CHECKING:
    from collections.abc import Iterator

runner = CliRunner()
EPIC = "dag-1"


@pytest.fixture
def in_tmp(tmp_path: Path) -> Iterator[Path]:
    """Run with cwd = a fresh repo root: skills and setup are cwd-based."""
    (tmp_path / ".git").mkdir()
    old = os.getcwd()
    os.chdir(tmp_path)
    try:
        yield tmp_path
    finally:
        os.chdir(old)


def _config(tmp_path: Path) -> DispatchConfig:
    cfg = DispatchConfig()
    cfg.project.repo = "o/r"
    cfg.logging.dir = str(tmp_path / "logs")
    cfg.branches.worktree_base = str(tmp_path / "wt")
    return cfg


def _document(result: Any) -> dict[str, Any]:
    """stdout parses as exactly one JSON object."""
    doc = json.loads(result.stdout)
    assert isinstance(doc, dict)
    assert isinstance(doc["schema"], int)
    return doc


# --- epic status ------------------------------------------------------------


def _epic_graph() -> Graph:
    return Graph(
        [
            Node(id="1", title="root", status=NodeStatus.DONE, epic_ref=EPIC),
            Node(id="2", title="middle", status=NodeStatus.TODO, epic_ref=EPIC),
            Node(id="3", title="leaf", status=NodeStatus.TODO, epic_ref=EPIC),
        ],
        [Edge("1", "2"), Edge("1", "3"), Edge("2", "3")],
    )


def _epic_summary() -> TickSummary:
    return TickSummary(
        counts={"done": 1, "todo": 1, "failed": 1},
        frontier=(),
        in_flight=(),
        held=(),
        failed=("2",),
        cancelled=(),
        stuck=(StuckNode(node_id="2", reason="failed-terminal", blast_radius=("3",)),),
        done=("1",),
        remaining=2,
        signal=TerminalSignal.STALLED,
        statuses={"1": "done", "2": "failed", "3": "todo"},
    )


def _invoke_epic_status(tmp_path: Path, *args: str) -> Any:
    with (
        patch("kew.cli.project_root", return_value=tmp_path),
        patch("kew.cli._load_epic_graph", return_value=_epic_graph()),
        patch("kew.cli.load_config", return_value=_config(tmp_path)),
        patch("kew.cli.preflight_tools"),
        patch("kew.cli.summarize", return_value=_epic_summary()),
    ):
        return runner.invoke(app, ["epic", "status", EPIC, *args])


def test_epic_status_json_lists_every_node(in_tmp: Path) -> None:
    result = _invoke_epic_status(in_tmp, "--json")
    assert result.exit_code == 0, result.output
    doc = _document(result)
    assert doc["epic"] == EPIC
    assert doc["signal"] == "stalled"
    nodes = {n["id"]: n for n in doc["nodes"]}
    assert set(nodes) == {"1", "2", "3"}
    assert nodes["1"] == {**nodes["1"], "title": "root", "status": "done"}
    assert nodes["2"]["status"] == "failed"  # the driver's reading, not the provider's
    assert nodes["3"]["dependencies"] == ["1", "2"]
    assert nodes["1"]["dependencies"] == []
    assert nodes["2"]["stuck"] == "failed-terminal"
    assert doc["stuck"] == [
        {"id": "2", "reason": "failed-terminal", "blast_radius": ["3"]}
    ]
    assert doc["done"] == ["1"]
    assert doc["remaining"] == 2


def test_epic_status_json_matches_the_text_report(in_tmp: Path) -> None:
    text = _invoke_epic_status(in_tmp)
    doc = _document(_invoke_epic_status(in_tmp, "--json"))
    assert "Signal: stalled" in text.stdout
    assert "Failed: ['2']" in text.stdout
    assert doc["failed"] == ["2"]
    assert doc["counts"] == {"done": 1, "todo": 1, "failed": 1}


def _invoke_epic_status_with(tmp_path: Path, summary: TickSummary) -> Any:
    with (
        patch("kew.cli.project_root", return_value=tmp_path),
        patch("kew.cli._load_epic_graph", return_value=_epic_graph()),
        patch("kew.cli.load_config", return_value=_config(tmp_path)),
        patch("kew.cli.preflight_tools"),
        patch("kew.cli.summarize", return_value=summary),
    ):
        return runner.invoke(app, ["epic", "status", EPIC])


def test_epic_status_text_lists_stuck_nodes_with_their_restart(in_tmp: Path) -> None:
    """The text view names each stuck node, its reason and restart — the
    lines the tick prints and --json carries (#2695)."""
    closed = "closed-unmerged: PR #9 closed without merging"
    summary = dataclasses.replace(
        _epic_summary(),
        stuck=(
            StuckNode(node_id="2", reason=closed, blast_radius=()),
            StuckNode(node_id="3", reason="failed-terminal", blast_radius=()),
        ),
    )
    result = _invoke_epic_status_with(in_tmp, summary)
    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    at = lines.index("Stuck:")
    assert lines[at + 1 : at + 3] == [
        f"  #2 {closed} — restart with `kew epic retry {EPIC} 2` "
        "(delete the closed PR's remote branch first if it still exists)",
        f"  #3 failed-terminal — `kew epic retry {EPIC} 3`",
    ]
    assert lines[-1] == "Signal: stalled"


def test_epic_status_text_keeps_a_stuck_nodes_blast_radius(in_tmp: Path) -> None:
    result = _invoke_epic_status(in_tmp)
    assert (
        f"Stuck:\n  #2 failed-terminal blast_radius=[#3] — `kew epic retry {EPIC} 2`"
        in result.stdout
    )


def test_epic_status_text_has_no_stuck_section_when_nothing_is_stuck(
    in_tmp: Path,
) -> None:
    summary = dataclasses.replace(_epic_summary(), stuck=())
    result = _invoke_epic_status_with(in_tmp, summary)
    assert result.exit_code == 0, result.output
    assert "Stuck:" not in result.output
    assert "stuck:" not in result.output
    assert "Signal: stalled" in result.stdout


def test_epic_status_json_falls_back_to_provider_status(in_tmp: Path) -> None:
    """An invalid graph's summary carries no per-node driver status."""
    bare = TickSummary(
        counts={},
        frontier=(),
        in_flight=(),
        held=(),
        failed=(),
        cancelled=(),
        stuck=(),
        done=(),
        remaining=3,
        signal=TerminalSignal.STALLED,
    )
    with (
        patch("kew.cli.project_root", return_value=in_tmp),
        patch("kew.cli._load_epic_graph", return_value=_epic_graph()),
        patch("kew.cli.load_config", return_value=_config(in_tmp)),
        patch("kew.cli.preflight_tools"),
        patch("kew.cli.summarize", return_value=bare),
    ):
        result = runner.invoke(app, ["epic", "status", EPIC, "--json"])
    assert result.exit_code == 0, result.output
    statuses = {n["id"]: n["status"] for n in _document(result)["nodes"]}
    assert statuses == {"1": "done", "2": "todo", "3": "todo"}


# --- skills list ------------------------------------------------------------


def test_skills_list_json_reports_name_state_and_drift(in_tmp: Path) -> None:
    runner.invoke(app, ["skills", "install", "handoff"])
    runner.invoke(app, ["skills", "install", "verify-parity"])
    (in_tmp / ".claude/skills/verify-parity/SKILL.md").write_text("edited\n")
    result = runner.invoke(app, ["skills", "list", "--json"])
    assert result.exit_code == 0, result.output
    doc = _document(result)
    by_name = {s["name"]: s for s in doc["skills"]}
    assert by_name["handoff"]["installed"] is True
    assert by_name["handoff"]["state"] == "current"
    assert by_name["handoff"]["drift"] is False
    assert by_name["verify-parity"]["installed"] is True
    assert by_name["verify-parity"]["state"] == "locally-modified"
    assert by_name["verify-parity"]["drift"] is True
    assert by_name["plan-to-issues"]["installed"] is False
    assert by_name["plan-to-issues"]["drift"] is False
    assert {"kind", "version", "target"} <= set(by_name["handoff"])


def test_skills_list_json_covers_the_text_rows(in_tmp: Path) -> None:
    text = runner.invoke(app, ["skills", "list"])
    doc = _document(runner.invoke(app, ["skills", "list", "--json"]))
    names = [s["name"] for s in doc["skills"]]
    assert names
    for name in names:
        assert name in text.stdout


# --- setup ------------------------------------------------------------------


def _fake_gh() -> Any:
    """`gh` that remembers labels: a second create of one fails, edit works."""
    existing: set[str] = set()

    def run(cmd: list[str], **_kw: Any) -> Any:
        rc = 0
        if cmd[1:3] == ["label", "create"]:
            rc = 1 if cmd[3] in existing else 0
            existing.add(cmd[3])
        return SimpleNamespace(returncode=rc, stdout='{"name": "r"}', stderr="")

    return run


def test_setup_json_reports_labels_hook_and_directories(in_tmp: Path) -> None:
    cfg = _config(in_tmp)
    with (
        patch("kew.cli.load_config", return_value=cfg),
        patch("kew.cli.preflight_tools"),
        patch("kew.cli._detect_repo"),
        patch("kew.labels.run", side_effect=_fake_gh()),
    ):
        first = runner.invoke(app, ["setup", "--json"])
        hook = (in_tmp / ".claude/hooks/security-guard.sh").read_text()
        second = runner.invoke(app, ["setup", "--json"])

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    one, two = _document(first), _document(second)
    assert one["repo"] == "o/r"
    assert one["labels"]["created"]
    assert one["labels"]["updated"] == []
    assert one["labels"]["failed"] == []
    assert one["hook_path"] == str(in_tmp / ".claude/hooks/security-guard.sh")
    assert one["directories"] == {
        "logs": str(in_tmp / "logs"),
        "worktrees": str(in_tmp / "wt"),
    }
    assert Path(one["directories"]["logs"]).is_dir()
    assert Path(one["directories"]["worktrees"]).is_dir()
    # Idempotent: the second run creates nothing, refreshes the same labels,
    # writes the same hook, and reports the same paths.
    assert two["labels"]["created"] == []
    assert sorted(two["labels"]["updated"]) == sorted(one["labels"]["created"])
    assert two["hook_path"] == one["hook_path"]
    assert two["directories"] == one["directories"]
    assert (in_tmp / ".claude/hooks/security-guard.sh").read_text() == hook
    assert two["ok"] is True


def test_setup_json_with_findings_still_prints_the_document(in_tmp: Path) -> None:
    def failing(cmd: list[str], **_kw: Any) -> Any:
        rc = 1 if cmd[1] == "label" else 0
        return SimpleNamespace(returncode=rc, stdout="", stderr="")

    with (
        patch("kew.cli.load_config", return_value=_config(in_tmp)),
        patch("kew.cli.preflight_tools"),
        patch("kew.cli._detect_repo"),
        patch("kew.labels.run", side_effect=failing),
    ):
        result = runner.invoke(app, ["setup", "--json"])
    assert result.exit_code == 1
    doc = _document(result)
    assert doc["labels"]["failed"]
    assert doc["ok"] is False


# --- triage queue -----------------------------------------------------------


def _record(store: SqliteRunStore, item: str, outcome: str, **kw: Any) -> None:
    store.record_triage_decision(
        item,
        body_digest=f"digest-{item}",
        backend="local",
        model="fake",
        prompt_sha="sha",
        outcome=outcome,
        decision_json="{}",
        **kw,
    )


def test_triage_queue_json_returns_the_text_items(in_tmp: Path) -> None:
    cfg = _config(in_tmp)
    db = in_tmp / "logs" / "dispatch.db"
    db.parent.mkdir(parents=True)
    with _open_run_store(db) as store:
        _record(
            store,
            "2293",
            "propose_dispatch",
            decision_type="feature",
            confidence=0.86,
            issue_num=2293,
        )
        _record(store, "2294", "unavailable")
    with patch("kew.cli.load_config", return_value=cfg):
        text = runner.invoke(app, ["triage", "queue"])
        result = runner.invoke(app, ["triage", "queue", "--json"])
    assert text.exit_code == 0, text.output
    assert result.exit_code == 0, result.output
    doc = _document(result)
    text_items = [
        line.split(":")[0] for line in text.stdout.splitlines() if line[:1].isdigit()
    ]
    assert [p["item_id"] for p in doc["proposals"]] == text_items
    assert sorted(text_items) == ["2293", "2294"]
    first = next(p for p in doc["proposals"] if p["item_id"] == "2293")
    assert first["outcome"] == "propose_dispatch"
    assert first["type"] == "feature"
    assert first["confidence"] == 0.86
    assert first["decide"] == "kew triage decide 2293"


def test_triage_queue_json_empty_queue_is_an_empty_list(in_tmp: Path) -> None:
    cfg = _config(in_tmp)
    db = in_tmp / "logs" / "dispatch.db"
    db.parent.mkdir(parents=True)
    with _open_run_store(db):
        pass
    with patch("kew.cli.load_config", return_value=cfg):
        result = runner.invoke(app, ["triage", "queue", "--json"])
    assert result.exit_code == 0, result.output
    assert _document(result)["proposals"] == []


def test_triage_queue_json_missing_ledger_still_exits_2(in_tmp: Path) -> None:
    with patch("kew.cli.load_config", return_value=_config(in_tmp)):
        result = runner.invoke(app, ["triage", "queue", "--json"])
    assert result.exit_code == 2
    assert result.stdout == ""
