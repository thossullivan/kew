"""Characterization / golden tests for cli.py::status and cli.py::_dispatch_one.

These tests pin the *current* output format and control flow of two central CLI
functions so that future refactors or incidental formatting changes produce a
visible diff rather than a silent regression.

Style rules (Node 4 spec):
- Snapshot/golden: assert on exact text fragments, not incidental whitespace.
- No changes to source functions in this PR.
- All GitHub/gh calls are mocked; only kew's own formatting and control flow
  are under test.
- Characterize what exists today; if something looks wrong, capture it as-is and
  leave a comment rather than fixing it.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from kew.cli import DispatchCommandResult, _dispatch_one, app
from kew.config import DispatchConfig
from kew.labels import ClaimResult
from kew.overlap import DispatchOutcome
from kew.run_store import RunRecord, SqliteRunStore

_runner = CliRunner()

# Fixed datetime used to freeze the CLI clock in tests that filter by "today".
# Using a mid-day instant avoids UTC-midnight flakiness: today_start will be
# 2026-07-10 00:00 UTC, and records seeded with _FIXED_NOW are clearly in range.
_FIXED_NOW = datetime(2026, 7, 10, 12, 0, 0, tzinfo=UTC)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_project_root(tmp_path: Path) -> None:
    # In-worktree temporary paths must not inherit the operator's ledger.
    (tmp_path / ".git").mkdir()


def _make_db(tmp_path: Path, records: list[RunRecord] | None = None) -> Path:
    """Create a dispatch.db under tmp_path/logs/ and optionally seed it."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    db_path = log_dir / "dispatch.db"
    with SqliteRunStore(str(db_path)) as store:
        for rec in records or []:
            store.save(rec)
    return db_path


def _cfg(tmp_path: Path) -> DispatchConfig:
    cfg = DispatchConfig()
    cfg.logging.dir = str(tmp_path / "logs")
    return cfg


def _issue(
    number: int = 1,
    title: str = "Test issue",
    labels: list[dict] | None = None,
) -> dict:
    return {
        "number": number,
        "title": title,
        "body": "Issue body",
        "labels": labels or [],
    }


# ---------------------------------------------------------------------------
# status — queue-count view (default)
# ---------------------------------------------------------------------------


class TestStatusQueueView:
    """Golden tests for the default `kew status` queue-count display."""

    @patch("kew.cli._preflight")
    @patch("kew.cli.load_config")
    def test_empty_fleet_shows_queue_header(
        self,
        mock_config: MagicMock,
        mock_preflight: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Empty fleet: header line present, no 'Recent runs' section."""
        cfg = _cfg(tmp_path)
        mock_config.return_value = cfg
        _make_db(tmp_path)

        with patch("kew.cli.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="0")
            result = _runner.invoke(app, ["status"])

        assert result.exit_code == 0
        assert "Queue status for" in result.output
        assert "Workers configured:" in result.output
        assert "Recent runs:" not in result.output
        # Each label line renders with its count (all 0 when gh returns "0")
        assert re.search(rf"{re.escape(cfg.labels.ready)}\s+0", result.output)

    @patch("kew.cli._preflight")
    @patch("kew.cli.load_config")
    def test_label_names_appear_in_queue_output(
        self,
        mock_config: MagicMock,
        mock_preflight: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Each configured label name is printed with its issue count."""
        cfg = _cfg(tmp_path)
        mock_config.return_value = cfg
        _make_db(tmp_path)

        with patch("kew.cli.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="3")
            result = _runner.invoke(app, ["status"])

        assert result.exit_code == 0
        # default label names from DispatchConfig
        assert cfg.labels.ready in result.output
        assert cfg.labels.in_progress in result.output
        assert cfg.labels.agent_failed in result.output
        assert cfg.labels.pr_pending_review in result.output
        # Each label line renders with its count (all 3 when gh returns "3")
        assert re.search(rf"{re.escape(cfg.labels.ready)}\s+3", result.output)

    @patch("kew.cli._preflight")
    @patch("kew.cli.load_config")
    def test_multiple_label_counts_rendered_independently(
        self,
        mock_config: MagicMock,
        mock_preflight: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Each queue label renders its own distinct count, not a shared value."""
        cfg = _cfg(tmp_path)
        mock_config.return_value = cfg
        _make_db(tmp_path)

        # Return different counts per gh call: ready=5, in-progress=2, rest=0
        mock_returns = [
            MagicMock(returncode=0, stdout="5"),
            MagicMock(returncode=0, stdout="2"),
            MagicMock(returncode=0, stdout="0"),
            MagicMock(returncode=0, stdout="1"),
            MagicMock(returncode=0, stdout="0"),
        ]
        with patch("kew.cli.run", side_effect=mock_returns):
            result = _runner.invoke(app, ["status"])

        assert result.exit_code == 0
        assert re.search(rf"{re.escape(cfg.labels.ready)}\s+5", result.output)
        assert re.search(rf"{re.escape(cfg.labels.in_progress)}\s+2", result.output)
        assert re.search(
            rf"{re.escape(cfg.labels.pr_pending_review)}\s+1", result.output
        )

    @patch("kew.cli._preflight")
    @patch("kew.cli.load_config")
    def test_running_issue_appears_in_recent_runs(
        self,
        mock_config: MagicMock,
        mock_preflight: MagicMock,
        tmp_path: Path,
    ) -> None:
        """A pending run started today shows in 'Recent runs' section."""
        cfg = _cfg(tmp_path)
        mock_config.return_value = cfg
        _make_db(
            tmp_path,
            [
                RunRecord(
                    run_id="run-pending-1",
                    issue_num=42,
                    branch="agent/issue-42",
                    status="pending",
                    exit_code=0,
                    started_at=_FIXED_NOW,
                )
            ],
        )

        with patch("kew.cli.run") as mock_run, patch("kew.cli.datetime") as mock_dt:
            mock_run.return_value = MagicMock(returncode=0, stdout="1")
            mock_dt.now.return_value = _FIXED_NOW
            result = _runner.invoke(app, ["status"])

        assert result.exit_code == 0
        assert "Recent runs:" in result.output
        assert "#42" in result.output
        assert "pending" in result.output

    @patch("kew.cli._preflight")
    @patch("kew.cli.load_config")
    def test_mixed_priorities_recent_runs_format(
        self,
        mock_config: MagicMock,
        mock_preflight: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Multiple runs today: each shows issue number, status, duration, cost."""
        cfg = _cfg(tmp_path)
        mock_config.return_value = cfg
        _make_db(
            tmp_path,
            [
                RunRecord(
                    run_id="run-ok",
                    issue_num=10,
                    branch="agent/issue-10",
                    status="success",
                    exit_code=0,
                    started_at=_FIXED_NOW,
                    duration_seconds=300.0,
                    estimated_cost_usd=1.23,
                ),
                RunRecord(
                    run_id="run-fail",
                    issue_num=11,
                    branch="agent/issue-11",
                    status="failure",
                    exit_code=1,
                    started_at=_FIXED_NOW,
                    duration_seconds=60.0,
                    estimated_cost_usd=0.07,
                ),
            ],
        )

        with patch("kew.cli.run") as mock_run, patch("kew.cli.datetime") as mock_dt:
            mock_run.return_value = MagicMock(returncode=0, stdout="0")
            mock_dt.now.return_value = _FIXED_NOW
            result = _runner.invoke(app, ["status"])

        assert result.exit_code == 0
        assert "Recent runs:" in result.output
        # Issue numbers appear
        assert "#10" in result.output
        assert "#11" in result.output
        # Status strings
        assert "success" in result.output
        assert "failure" in result.output
        # Cost values formatted as $X.XX
        assert "$1.23" in result.output
        assert "$0.07" in result.output

    @patch("kew.cli._preflight")
    @patch("kew.cli.load_config")
    def test_retry_attempt_number_shown(
        self,
        mock_config: MagicMock,
        mock_preflight: MagicMock,
        tmp_path: Path,
    ) -> None:
        """A run with attempt_number > 1 shows '(attempt N)' in the output."""
        cfg = _cfg(tmp_path)
        mock_config.return_value = cfg
        _make_db(
            tmp_path,
            [
                RunRecord(
                    run_id="run-retry",
                    issue_num=99,
                    branch="agent/issue-99",
                    status="success",
                    exit_code=0,
                    started_at=_FIXED_NOW,
                    attempt_number=2,
                    estimated_cost_usd=0.50,
                )
            ],
        )

        with patch("kew.cli.run") as mock_run, patch("kew.cli.datetime") as mock_dt:
            mock_run.return_value = MagicMock(returncode=0, stdout="0")
            mock_dt.now.return_value = _FIXED_NOW
            result = _runner.invoke(app, ["status"])

        assert result.exit_code == 0
        assert "(attempt 2)" in result.output

    @patch("kew.cli._preflight")
    @patch("kew.cli.load_config")
    def test_history_flag_shows_cost_section(
        self,
        mock_config: MagicMock,
        mock_preflight: MagicMock,
        tmp_path: Path,
    ) -> None:
        """--history adds a 'Cost: $X today | $Y this week' line."""
        cfg = _cfg(tmp_path)
        mock_config.return_value = cfg
        _make_db(
            tmp_path,
            [
                RunRecord(
                    run_id="run-history",
                    issue_num=7,
                    branch="agent/issue-7",
                    status="success",
                    exit_code=0,
                    started_at=_FIXED_NOW,
                    estimated_cost_usd=2.00,
                )
            ],
        )

        with patch("kew.cli.run") as mock_run, patch("kew.cli.datetime") as mock_dt:
            mock_run.return_value = MagicMock(returncode=0, stdout="0")
            mock_dt.now.return_value = _FIXED_NOW
            result = _runner.invoke(app, ["status", "--history"])

        assert result.exit_code == 0
        assert "Cost:" in result.output
        assert "today" in result.output
        assert "this week" in result.output

    @patch("kew.cli._preflight")
    @patch("kew.cli.load_config")
    def test_cost_flag_shows_budget_when_configured(
        self,
        mock_config: MagicMock,
        mock_preflight: MagicMock,
        tmp_path: Path,
    ) -> None:
        """--cost with a daily_usd budget shows 'Budget: $X/day (Y% used)'."""
        from kew.config import BudgetConfig

        cfg = _cfg(tmp_path)
        cfg.budget = BudgetConfig(daily_usd=50.0)
        mock_config.return_value = cfg
        _make_db(
            tmp_path,
            [
                RunRecord(
                    run_id="run-budget",
                    issue_num=5,
                    branch="agent/issue-5",
                    status="success",
                    exit_code=0,
                    started_at=_FIXED_NOW,
                    estimated_cost_usd=10.0,
                )
            ],
        )

        with patch("kew.cli.run") as mock_run, patch("kew.cli.datetime") as mock_dt:
            mock_run.return_value = MagicMock(returncode=0, stdout="0")
            mock_dt.now.return_value = _FIXED_NOW
            result = _runner.invoke(app, ["status", "--cost"])

        assert result.exit_code == 0
        assert "Budget:" in result.output
        assert "$50.00/day" in result.output
        assert "20.0%" in result.output

    @patch("kew.cli._preflight")
    @patch("kew.cli.load_config")
    def test_fleet_stopped_banner_in_default_view(
        self,
        mock_config: MagicMock,
        mock_preflight: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Fleet kill-switch banner is emitted before queue counts."""
        cfg = _cfg(tmp_path)
        mock_config.return_value = cfg
        log_dir = tmp_path / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        db_path = log_dir / "dispatch.db"
        with SqliteRunStore(str(db_path)) as store:
            store.set_fleet_stopped(reason="emergency stop")

        with patch("kew.cli.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="0")
            result = _runner.invoke(app, ["status"])

        assert result.exit_code == 0
        assert "***" in result.output


# ---------------------------------------------------------------------------
# status — run_id lookup view
# ---------------------------------------------------------------------------


class TestStatusRunLookup:
    """Golden tests for `kew status --run <id>` single-run detail view."""

    @patch("kew.cli._preflight")
    @patch("kew.cli.load_config")
    def test_run_detail_shows_all_fields(
        self,
        mock_config: MagicMock,
        mock_preflight: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Detail view prints Run/Issue/Branch/Status/Exit/Started/Duration/Cost."""
        cfg = _cfg(tmp_path)
        mock_config.return_value = cfg
        started = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
        finished = datetime(2026, 6, 1, 12, 10, 0, tzinfo=UTC)
        _make_db(
            tmp_path,
            [
                RunRecord(
                    run_id="detail-run-1",
                    issue_num=55,
                    branch="agent/issue-55",
                    status="success",
                    exit_code=0,
                    started_at=started,
                    finished_at=finished,
                    duration_seconds=600.0,
                    estimated_cost_usd=3.14,
                    input_tokens=10000,
                    output_tokens=4000,
                )
            ],
        )

        result = _runner.invoke(app, ["status", "--run", "detail-run-1"])

        assert result.exit_code == 0
        assert "detail-run-1" in result.output
        assert "#55" in result.output
        assert "agent/issue-55" in result.output
        assert "success" in result.output
        assert "$3.14" in result.output
        assert "10000" in result.output
        assert "4000" in result.output
        assert "Exit:" in result.output
        assert "Started:" in result.output
        assert "600s" in result.output

    @patch("kew.cli._preflight")
    @patch("kew.cli.load_config")
    def test_unknown_run_id_exits_2(
        self,
        mock_config: MagicMock,
        mock_preflight: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Unknown run ID emits error to stderr and exits 2."""
        cfg = _cfg(tmp_path)
        mock_config.return_value = cfg
        _make_db(tmp_path)

        result = _runner.invoke(app, ["status", "--run", "no-such-run"])

        assert result.exit_code == 2
        assert "not found" in result.output

    @patch("kew.cli._preflight")
    @patch("kew.cli.load_config")
    def test_run_detail_shows_error_category_when_present(
        self,
        mock_config: MagicMock,
        mock_preflight: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Detail view includes 'Error: <category>' line when error_category set."""
        cfg = _cfg(tmp_path)
        mock_config.return_value = cfg
        _make_db(
            tmp_path,
            [
                RunRecord(
                    run_id="err-run",
                    issue_num=66,
                    branch="agent/issue-66",
                    status="failure",
                    exit_code=1,
                    started_at=datetime.now(UTC),
                    error_category="budget_exceeded",
                )
            ],
        )

        result = _runner.invoke(app, ["status", "--run", "err-run"])

        assert result.exit_code == 0
        assert "budget_exceeded" in result.output


class TestStatusRunWorkProjection:
    """Golden tests for the canonical work block `kew status --run` appends.

    Pinned because this block is the CLI's only rendering of the shared
    projection: a silent change to a label here is a silent change to what a
    scripted operator believes about preserved work.
    """

    @staticmethod
    def _admit(tmp_path: Path, **overrides: object) -> None:
        from kew.run_store import AdmissionIdentity, SqliteRunStore

        fields: dict = {
            "run_id": "work-run-1",
            "issue_num": 0,
            "item_id": "SFD-12",
            "branch": "agent/issue-sfd-12",
            "status": "awaiting_publication",
            "exit_code": 0,
            "started_at": datetime(2026, 8, 18, 10, 0, tzinfo=UTC),
            "finished_at": datetime(2026, 8, 18, 10, 30, tzinfo=UTC),
            "runner_name": "claude-code",
            "model": "claude-opus-5",
            "attempt_number": 1,
        }
        fields.update(overrides)
        log_dir = tmp_path / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        with SqliteRunStore(str(log_dir / "dispatch.db")) as store:
            store.admit_run_with_identity(
                RunRecord(**fields),
                AdmissionIdentity(
                    run_id=str(fields["run_id"]),
                    attempt_number=1,
                    source_tracker="linear",
                    item_id=str(fields["item_id"]),
                    delivery_repo="acme/widgets",
                ),
            )

    @patch("kew.cli._preflight")
    @patch("kew.cli.load_config")
    def test_work_block_labels_are_pinned(
        self, mock_config: MagicMock, mock_preflight: MagicMock, tmp_path: Path
    ) -> None:
        """Every label in the appended work block, in order."""
        mock_config.return_value = _cfg(tmp_path)
        self._admit(tmp_path)

        result = _runner.invoke(app, ["status", "--run", "work-run-1"])

        assert result.exit_code == 0
        labels = [
            "Item:",
            "Revision:",
            "Execution:",
            "Artifact:",
            "Judgment:",
            "Source:",
            "Route:",
            "Exception:",
            "Evidence:",
            "Next:",
        ]
        offsets = [result.output.find(label) for label in labels]
        assert all(o >= 0 for o in offsets), dict(zip(labels, offsets, strict=True))
        assert offsets == sorted(offsets)

    @patch("kew.cli._preflight")
    @patch("kew.cli.load_config")
    def test_preserved_linear_work_reads_as_preserved_not_failed(
        self, mock_config: MagicMock, mock_preflight: MagicMock, tmp_path: Path
    ) -> None:
        mock_config.return_value = _cfg(tmp_path)
        self._admit(tmp_path)

        result = _runner.invoke(app, ["status", "--run", "work-run-1"])

        assert "Item:     SFD-12" in result.output
        assert "preserved" in result.output
        assert "finalize_publication" in result.output
        assert "ERR" not in result.output

    @patch("kew.cli._preflight")
    @patch("kew.cli.load_config")
    def test_legacy_github_record_still_renders_its_hash_ref(
        self, mock_config: MagicMock, mock_preflight: MagicMock, tmp_path: Path
    ) -> None:
        """A row written before admission identity existed keeps ``#N``."""
        mock_config.return_value = _cfg(tmp_path)
        _make_db(
            tmp_path,
            [
                RunRecord(
                    run_id="legacy-1",
                    issue_num=55,
                    branch="agent/issue-55",
                    status="success",
                    exit_code=0,
                    started_at=datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC),
                )
            ],
        )

        result = _runner.invoke(app, ["status", "--run", "legacy-1"])

        assert result.exit_code == 0
        assert "Item:     #55" in result.output


# ---------------------------------------------------------------------------
# status — JSON export
# ---------------------------------------------------------------------------


class TestStatusJsonExport:
    """Golden tests for `kew status --json` structured output."""

    @patch("kew.cli._preflight")
    @patch("kew.cli.list_ready_issues")
    @patch("kew.cli.load_config")
    def test_json_shape_top_level_keys(
        self,
        mock_config: MagicMock,
        mock_queue: MagicMock,
        mock_preflight: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Export JSON has top-level keys: runs, queue, workers, budget."""
        cfg = _cfg(tmp_path)
        mock_config.return_value = cfg
        mock_queue.return_value = []
        _make_db(tmp_path)

        result = _runner.invoke(app, ["status", "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert set(data.keys()) >= {"runs", "queue", "workers", "budget"}

    @patch("kew.cli._preflight")
    @patch("kew.cli.list_ready_issues")
    @patch("kew.cli.load_config")
    def test_json_run_record_fields(
        self,
        mock_config: MagicMock,
        mock_queue: MagicMock,
        mock_preflight: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Each run entry in JSON has the expected field set."""
        cfg = _cfg(tmp_path)
        mock_config.return_value = cfg
        mock_queue.return_value = []
        started = datetime(2026, 5, 1, 9, 0, 0, tzinfo=UTC)
        _make_db(
            tmp_path,
            [
                RunRecord(
                    run_id="json-run-1",
                    issue_num=20,
                    branch="agent/issue-20",
                    status="success",
                    exit_code=0,
                    started_at=started,
                    duration_seconds=120.0,
                    estimated_cost_usd=0.88,
                    input_tokens=500,
                    output_tokens=200,
                    attempt_number=1,
                )
            ],
        )

        result = _runner.invoke(app, ["status", "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data["runs"]) == 1
        rec = data["runs"][0]
        expected_fields = {
            "run_id",
            "issue_num",
            "branch",
            "status",
            "exit_code",
            "started_at",
            "finished_at",
            "duration_seconds",
            "estimated_cost_usd",
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "attempt_number",
        }
        assert expected_fields <= set(rec.keys())
        assert rec["run_id"] == "json-run-1"
        assert rec["issue_num"] == 20
        assert rec["status"] == "success"
        assert rec["estimated_cost_usd"] == pytest.approx(0.88)

    @patch("kew.cli._preflight")
    @patch("kew.cli.list_ready_issues")
    @patch("kew.cli.load_config")
    def test_json_queue_shape(
        self,
        mock_config: MagicMock,
        mock_queue: MagicMock,
        mock_preflight: MagicMock,
        tmp_path: Path,
    ) -> None:
        """queue.count and queue.issues are populated from list_ready_issues."""
        cfg = _cfg(tmp_path)
        mock_config.return_value = cfg
        mock_queue.return_value = [
            {
                "number": 33,
                "title": "Ready issue",
                "labels": [{"name": "ready"}, {"name": "medium"}],
                "createdAt": "2026-06-01T00:00:00Z",
            }
        ]
        _make_db(tmp_path)

        result = _runner.invoke(app, ["status", "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["queue"]["count"] == 1
        issue = data["queue"]["issues"][0]
        assert issue["number"] == 33
        assert issue["title"] == "Ready issue"
        assert "ready" in issue["labels"]
        assert "medium" in issue["labels"]

    @patch("kew.cli._preflight")
    @patch("kew.cli.list_ready_issues")
    @patch("kew.cli.load_config")
    def test_json_workers_lists_pending_runs(
        self,
        mock_config: MagicMock,
        mock_queue: MagicMock,
        mock_preflight: MagicMock,
        tmp_path: Path,
    ) -> None:
        """workers array contains only pending runs with run_id/issue_num/started_at."""
        cfg = _cfg(tmp_path)
        mock_config.return_value = cfg
        mock_queue.return_value = []
        now = datetime.now(UTC)
        _make_db(
            tmp_path,
            [
                RunRecord(
                    run_id="pend-1",
                    issue_num=77,
                    branch="agent/issue-77",
                    status="pending",
                    exit_code=0,
                    started_at=now,
                    runner_name="claude-code",
                    model="claude-sonnet-4-6",
                ),
                RunRecord(
                    run_id="done-1",
                    issue_num=78,
                    branch="agent/issue-78",
                    status="success",
                    exit_code=0,
                    started_at=now,
                ),
            ],
        )

        result = _runner.invoke(app, ["status", "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        workers = data["workers"]
        assert len(workers) == 1
        w = workers[0]
        assert w["run_id"] == "pend-1"
        assert w["issue_num"] == 77
        assert "started_at" in w

    @patch("kew.cli._preflight")
    @patch("kew.cli.list_ready_issues")
    @patch("kew.cli.load_config")
    def test_json_budget_fields(
        self,
        mock_config: MagicMock,
        mock_queue: MagicMock,
        mock_preflight: MagicMock,
        tmp_path: Path,
    ) -> None:
        """budget block contains daily_spend, daily/weekly/per-issue limits."""
        from kew.config import BudgetConfig

        cfg = _cfg(tmp_path)
        cfg.budget = BudgetConfig(daily_usd=25.0, weekly_usd=100.0, per_issue_usd=5.0)
        mock_config.return_value = cfg
        mock_queue.return_value = []
        _make_db(tmp_path)

        result = _runner.invoke(app, ["status", "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        b = data["budget"]
        assert b["daily_limit"] == 25.0
        assert b["weekly_limit"] == 100.0
        assert b["per_issue_limit"] == 5.0
        assert "daily_spend" in b

    @patch("kew.cli._preflight")
    @patch("kew.cli.list_ready_issues")
    @patch("kew.cli.load_config")
    def test_json_fleet_stopped_not_in_output(
        self,
        mock_config: MagicMock,
        mock_queue: MagicMock,
        mock_preflight: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Fleet kill-switch banner is suppressed in JSON export (output valid JSON)."""
        cfg = _cfg(tmp_path)
        mock_config.return_value = cfg
        mock_queue.return_value = []
        log_dir = tmp_path / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        db_path = log_dir / "dispatch.db"
        with SqliteRunStore(str(db_path)) as store:
            store.set_fleet_stopped(reason="test stop")

        result = _runner.invoke(app, ["status", "--json"])

        assert result.exit_code == 0
        # Must be valid JSON — banner must not have been prepended
        data = json.loads(result.output)
        assert "runs" in data

    @patch("kew.cli._preflight")
    @patch("kew.cli.load_config")
    def test_unsupported_export_format_exits_2(
        self,
        mock_config: MagicMock,
        mock_preflight: MagicMock,
        tmp_path: Path,
    ) -> None:
        """The removed --export flag is a usage error (exit 2); --json replaces it."""
        cfg = _cfg(tmp_path)
        mock_config.return_value = cfg
        _make_db(tmp_path)

        result = _runner.invoke(app, ["status", "--export", "json"])

        assert result.exit_code == 2
        assert "No such option '--export'" in re.sub(
            r"\x1b\[[0-9;]*m", "", result.output
        )


# ---------------------------------------------------------------------------
# _dispatch_one — control flow characterization
# ---------------------------------------------------------------------------


class TestDispatchOneEmptyQueue:
    """_dispatch_one returns an empty result when the selector finds nothing."""

    @patch("kew.cli.select_next_issue", return_value=None)
    def test_returns_empty_on_empty_queue(self, mock_select: MagicMock) -> None:
        config = DispatchConfig()
        rc = _dispatch_one(config, force_issue=None, dry_run=False)
        assert rc == DispatchCommandResult("empty", 0)
        mock_select.assert_called_once()


class TestDispatchOneGhFailure:
    """A gh selection failure must be loud, with a distinct exit code (#SFD-151)."""

    @patch("kew.cli.select_next_issue")
    def test_returns_distinct_code_on_gh_failure(
        self, mock_select: MagicMock, capsys
    ) -> None:
        from kew.cli import _DISPATCH_GH_QUERY_FAILURE
        from kew.selector import GhQueryError

        mock_select.side_effect = GhQueryError("gh exited 1: token expired")
        config = DispatchConfig()
        rc = _dispatch_one(config, force_issue=None, dry_run=False)
        assert rc == _DISPATCH_GH_QUERY_FAILURE == DispatchCommandResult("query_failed")
        assert rc != 2  # never mistaken for an empty queue
        assert "GitHub query failed" in capsys.readouterr().err


class TestDispatchOneDryRun:
    """_dispatch_one --dry-run prints a successful preview without dispatching."""

    # An admissible issue: the preview reports the same admission checks the real
    # run applies, so it needs the ready label and a free branch name to promise
    # a dispatch. Either way the preview never dispatches (#2343 collapsed the
    # two one-assertion near-copies into this one case per admission verdict).
    @pytest.mark.parametrize(
        ("labels", "expected"),
        [
            pytest.param(
                [{"name": "ready"}],
                DispatchCommandResult("executed", 0),
                id="admissible",
            ),
            pytest.param([], DispatchCommandResult("refused", 0), id="not-admissible"),
        ],
    )
    @patch("kew.cli.execute_dispatch")
    @patch("kew.cli.remote_branch_exists", return_value=False)
    @patch("kew.cli.select_next_issue")
    def test_dry_run_previews_and_never_dispatches(
        self,
        mock_select: MagicMock,
        _branch: MagicMock,
        mock_execute: MagicMock,
        labels: list[dict],
        expected: DispatchCommandResult,
        capsys,
    ) -> None:
        mock_select.return_value = _issue(number=5, title="Dry run test", labels=labels)
        config = DispatchConfig()
        rc = _dispatch_one(config, force_issue=5, dry_run=True)
        assert rc == expected
        captured = capsys.readouterr()
        # _success/_dim write to stderr; assert the preview appears
        assert "[DRY RUN]" in captured.err
        assert "Dry run test" in captured.err
        mock_execute.assert_not_called()


class TestDispatchOneShowPrompt:
    """_dispatch_one --show-prompt returns a successful prompt preview."""

    @patch("kew.cli.execute_dispatch")
    @patch("kew.cli.select_next_issue")
    def test_show_prompt_returns_0(
        self, mock_select: MagicMock, mock_execute: MagicMock, capsys
    ) -> None:
        mock_select.return_value = _issue(number=3, title="Prompt test")
        config = DispatchConfig()
        rc = _dispatch_one(config, force_issue=3, dry_run=False, show_prompt=True)
        assert rc == DispatchCommandResult("executed", 0)
        captured = capsys.readouterr()
        # typer.echo(prompt) writes the full prompt to stdout; title is embedded in it
        assert "Prompt test" in captured.out
        mock_execute.assert_not_called()

    @patch("kew.cli.select_next_issue")
    def test_codex_show_prompt_is_host_publication(
        self, mock_select: MagicMock, capsys
    ) -> None:
        mock_select.return_value = _issue(number=3, title="Codex preview")
        config = DispatchConfig()
        config.agent.runner = "codex"
        config.agent.model = "gpt-5.6-sol"
        assert _dispatch_one(
            config, force_issue=3, dry_run=False, show_prompt=True
        ) == DispatchCommandResult("executed")
        prompt = capsys.readouterr().out
        assert "Only edit files and run verification" in prompt
        assert "gh pr create" not in prompt


class TestDispatchOneDuplicateBranch:
    """_dispatch_one refuses a named issue whose remote branch already exists."""

    @patch("kew.cli.execute_dispatch")
    @patch("kew.cli.remote_branch_exists", return_value=True)
    @patch("kew.cli.select_next_issue")
    def test_returns_refused_when_branch_exists(
        self, mock_select: MagicMock, mock_branch: MagicMock, mock_execute: MagicMock
    ) -> None:
        mock_select.return_value = _issue(number=9)
        config = DispatchConfig()
        rc = _dispatch_one(config, force_issue=9, dry_run=False)
        assert rc == DispatchCommandResult("refused", 0)
        mock_execute.assert_not_called()


class TestDispatchOneClaimRefusal:
    """_dispatch_one preserves the reason try_claim_issue refuses to claim."""

    @pytest.mark.parametrize(
        ("claim_result", "expected"),
        [
            pytest.param(
                ClaimResult.NOT_READY,
                DispatchCommandResult("refused", 0),
                id="not-ready",
            ),
            pytest.param(
                ClaimResult.ALREADY_CLAIMED,
                DispatchCommandResult("refused", 0),
                id="already-claimed",
            ),
            pytest.param(
                ClaimResult.RACE_DETECTED,
                DispatchCommandResult("query_failed"),
                id="race-detected",
            ),
        ],
    )
    @patch("kew.cli.execute_dispatch")
    @patch("kew.cli.remote_branch_exists", return_value=False)
    @patch("kew.cli.select_next_issue")
    def test_every_claim_refusal_keeps_its_own_result_and_never_dispatches(
        self,
        mock_select: MagicMock,
        mock_branch: MagicMock,
        mock_execute: MagicMock,
        claim_result: ClaimResult,
        expected: DispatchCommandResult,
    ) -> None:
        mock_select.return_value = _issue(number=12, labels=[{"name": "blocked"}])
        config = DispatchConfig()
        with patch("kew.cli.try_claim_issue", return_value=claim_result):
            rc = _dispatch_one(config, force_issue=12, dry_run=False)
        assert rc == expected
        mock_execute.assert_not_called()


class TestDispatchOneSuccessfulDispatch:
    """_dispatch_one returns execute_dispatch's exit_code on success."""

    @patch("kew.cli.execute_dispatch", return_value=DispatchOutcome(exit_code=0))
    @patch("kew.cli.remote_branch_exists", return_value=False)
    @patch("kew.cli.try_claim_issue", return_value=ClaimResult.CLAIMED)
    @patch("kew.cli.select_next_issue")
    def test_returns_0_on_success(
        self,
        mock_select: MagicMock,
        mock_claim: MagicMock,
        mock_branch: MagicMock,
        mock_execute: MagicMock,
    ) -> None:
        mock_select.return_value = _issue(number=100, title="Success issue")
        config = DispatchConfig()
        rc = _dispatch_one(config, force_issue=100, dry_run=False)
        assert rc == DispatchCommandResult("executed", 0)
        mock_execute.assert_called_once()

    @patch("kew.cli.execute_dispatch", return_value=DispatchOutcome(exit_code=0))
    @patch("kew.cli.remote_branch_exists", return_value=False)
    @patch("kew.cli.try_claim_issue", return_value=ClaimResult.CLAIMED)
    @patch("kew.cli.select_next_issue")
    def test_execute_receives_issue_and_config(
        self,
        mock_select: MagicMock,
        mock_claim: MagicMock,
        mock_branch: MagicMock,
        mock_execute: MagicMock,
    ) -> None:
        issue_data = _issue(number=101, title="Config pass-through")
        mock_select.return_value = issue_data
        config = DispatchConfig()
        _dispatch_one(config, force_issue=101, dry_run=False)
        kwargs = mock_execute.call_args.kwargs
        assert kwargs["issue"] == issue_data
        assert kwargs["config"] is config
        # force_issue is forwarded to the selector
        mock_select.assert_called_once_with(config=config, force_issue=101)

    @patch("kew.cli.execute_dispatch", return_value=DispatchOutcome(exit_code=0))
    @patch("kew.cli.remote_branch_exists", return_value=False)
    @patch("kew.cli.try_claim_issue", return_value=ClaimResult.CLAIMED)
    @patch("kew.cli.select_next_issue")
    def test_store_passed_to_execute_dispatch(
        self,
        mock_select: MagicMock,
        mock_claim: MagicMock,
        mock_branch: MagicMock,
        mock_execute: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Store object is forwarded into execute_dispatch."""
        mock_select.return_value = _issue(number=102)
        config = DispatchConfig()
        db_path = tmp_path / "dispatch.db"
        with SqliteRunStore(str(db_path)) as store:
            _dispatch_one(config, force_issue=102, dry_run=False, store=store)
        kwargs = mock_execute.call_args.kwargs
        assert kwargs["store"] is store


class TestDispatchOneFailedDispatch:
    """_dispatch_one forwards non-zero exit codes from execute_dispatch."""

    @patch("kew.cli.execute_dispatch", return_value=DispatchOutcome(exit_code=1))
    @patch("kew.cli.remote_branch_exists", return_value=False)
    @patch("kew.cli.try_claim_issue", return_value=ClaimResult.CLAIMED)
    @patch("kew.cli.select_next_issue")
    def test_returns_executed_failure(
        self,
        mock_select: MagicMock,
        mock_claim: MagicMock,
        mock_branch: MagicMock,
        mock_execute: MagicMock,
    ) -> None:
        mock_select.return_value = _issue(number=200, title="Failing issue")
        config = DispatchConfig()
        rc = _dispatch_one(config, force_issue=200, dry_run=False)
        assert rc == DispatchCommandResult("executed", 1)

    @patch("kew.cli.execute_dispatch", return_value=DispatchOutcome(exit_code=1))
    @patch("kew.cli.remote_branch_exists", return_value=False)
    @patch("kew.cli.try_claim_issue", return_value=ClaimResult.CLAIMED)
    @patch("kew.cli.select_next_issue")
    def test_model_override_forwarded(
        self,
        mock_select: MagicMock,
        mock_claim: MagicMock,
        mock_branch: MagicMock,
        mock_execute: MagicMock,
    ) -> None:
        """--model flag reaches execute_dispatch as model_override."""
        mock_select.return_value = _issue(number=201)
        config = DispatchConfig()
        _dispatch_one(config, force_issue=201, dry_run=False, model="claude-opus-4-8")
        kwargs = mock_execute.call_args.kwargs
        assert kwargs["model_override"] == "claude-opus-4-8"


class TestDispatchOneMalformedIssue:
    """_dispatch_one returns query_failed when required issue fields are missing."""

    @patch("kew.cli.select_next_issue")
    def test_missing_number_returns_query_failed(self, mock_select: MagicMock) -> None:
        mock_select.return_value = {"title": "No number", "body": "", "labels": []}
        config = DispatchConfig()
        rc = _dispatch_one(config, force_issue=None, dry_run=False)
        assert rc == DispatchCommandResult("query_failed")

    @patch("kew.cli.select_next_issue")
    def test_missing_title_returns_query_failed(self, mock_select: MagicMock) -> None:
        mock_select.return_value = {"number": 1, "body": "", "labels": []}
        config = DispatchConfig()
        rc = _dispatch_one(config, force_issue=None, dry_run=False)
        assert rc == DispatchCommandResult("query_failed")
