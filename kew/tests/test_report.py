"""Tests for kew.report — the `kew report` spend-report feature."""

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from kew.cli import app
from kew.config import BudgetConfig, DispatchConfig
from kew.outcomes import SyncResult
from kew.report import (
    build_report,
    compute_stats,
    render_report,
    resolve_pr_states,
)
from kew.run_store import RunRecord, SqliteRunStore

_BASE64_PAYLOAD = re.compile(r"base64,[A-Za-z0-9+/=]+")


@pytest.fixture(autouse=True)
def _report_project_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep report ledger registries out of the real checkout."""
    (tmp_path / ".git").mkdir(exist_ok=True)
    monkeypatch.setattr("kew.report.project_root", lambda: tmp_path)


def _without_base64(html: str) -> str:
    """Blank out inlined data-URI payloads before substring assertions."""
    return _BASE64_PAYLOAD.sub("base64,", html)


class TestResolvePrStates:
    def test_empty_url_list_makes_no_gh_call(self):
        with patch("kew.report.run") as mock_run:
            assert resolve_pr_states([]) == {}
            mock_run.assert_not_called()

    def test_batches_all_urls_into_at_most_two_gh_calls(self):
        """Acceptance criterion 3: <=2 gh invocations regardless of PR count."""
        urls = [f"https://github.com/acme/widgets/pull/{n}" for n in range(1, 31)]
        payload = {
            "data": {
                f"pr{i}": {"url": url, "state": "MERGED"} for i, url in enumerate(urls)
            }
        }
        with patch("kew.report.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps(payload))
            states = resolve_pr_states(urls)
        assert mock_run.call_count <= 2
        assert states is not None
        assert len(states) == 30

    def test_parses_states_and_builds_graphql_query(self):
        urls = [
            "https://github.com/acme/widgets/pull/201",
            "https://github.com/acme/widgets/pull/205",
        ]
        payload = {
            "data": {
                "pr0": {"url": urls[0], "state": "MERGED"},
                "pr1": {"url": urls[1], "state": "OPEN"},
            }
        }
        with patch("kew.report.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps(payload))
            states = resolve_pr_states(urls)
        assert states == {urls[0]: "MERGED", urls[1]: "OPEN"}
        cmd = mock_run.call_args[0][0]
        assert cmd[:3] == ["gh", "api", "graphql"]
        assert urls[0] in cmd[-1]
        assert urls[1] in cmd[-1]

    def test_nonzero_exit_returns_none(self):
        with patch("kew.report.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="boom")
            assert resolve_pr_states(["https://github.com/a/b/pull/1"]) is None

    def test_oserror_returns_none(self):
        with patch("kew.report.run", side_effect=OSError("network down")):
            assert resolve_pr_states(["https://github.com/a/b/pull/1"]) is None

    def test_gh_missing_from_allowlist_returns_none(self):
        """resolve_cmd raises ValueError when gh is not in TOOL_PATHS."""
        with patch("kew.report.run", side_effect=ValueError("not in allowlist")):
            assert resolve_pr_states(["https://github.com/a/b/pull/1"]) is None

    def test_malformed_json_returns_none(self):
        with patch("kew.report.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="not json")
            assert resolve_pr_states(["https://github.com/a/b/pull/1"]) is None

    def test_null_node_for_deleted_pr_is_skipped(self):
        payload = {"data": {"pr0": None}}
        with patch("kew.report.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps(payload))
            assert resolve_pr_states(["https://github.com/a/b/pull/9"]) == {}


def _record(
    run_id: str,
    issue_num: int,
    *,
    status: str = "success",
    cost: float = 1.0,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_creation_tokens: int = 0,
    cache_read_tokens: int = 0,
    model: str = "claude-sonnet-4-6",
    runner_name: str = "claude-code",
    title: str = "",
    pr_url: str | None = None,
    error_category: str = "",
    started_at: datetime | None = None,
    item_id: str | None = None,
    run_kind: str = "dispatch",
) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        issue_num=issue_num,
        item_id=item_id,
        branch=f"agent/issue-{issue_num}",
        status=status,  # type: ignore[arg-type]
        exit_code=0 if status == "success" else 1,
        run_kind=run_kind,
        started_at=started_at or (datetime.now(UTC) - timedelta(days=1)),
        total_tokens=(
            input_tokens + output_tokens + cache_creation_tokens + cache_read_tokens
        ),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_tokens=cache_creation_tokens,
        cache_read_tokens=cache_read_tokens,
        estimated_cost_usd=cost,
        error_category=error_category,
        pr_url=pr_url,
        runner_name=runner_name,
        model=model,
        title=title,
    )


class TestComputeStats:
    def test_spend_and_status_counts_with_killed_mapping(self):
        records = [
            _record("r1", 1, status="success", cost=4.0),
            _record("r2", 2, status="failure", cost=3.0, error_category="permanent"),
            _record(
                "r3", 3, status="failure", cost=0.5, error_category="stale_pending"
            ),
            _record("r4", 4, status="budget_exceeded", cost=6.0),
            _record("r5", 5, status="pending", cost=0.2),
        ]
        stats = compute_stats(records, pr_states={})
        assert stats.total_spend == 13.7
        assert stats.run_count == 5
        assert stats.success_count == 1
        assert stats.failure_count == 1  # excludes the stale_pending one
        assert stats.killed_count == 1  # failure + error_category=stale_pending
        assert stats.budget_exceeded_count == 1
        assert stats.pending_count == 1
        # No review-kind runs in this window → the review pool bucket is $0.
        assert stats.review_spend == 0.0

    def test_review_spend_is_its_own_run_kind_bucket(self):
        records = [
            _record("impl", 1, cost=4.0, run_kind="dispatch"),
            _record("rev", 1, cost=1.25, run_kind="adversarial_review"),
            _record("rem", 1, cost=0.75, run_kind="review_remediation"),
        ]
        stats = compute_stats(records, pr_states={})
        # total_spend covers everything; review_spend isolates the review pool
        # so heavyweight review runs are visible without polluting the headline.
        assert stats.total_spend == 6.0
        assert stats.review_spend == 2.0

    def test_token_totals(self):
        records = [
            _record("r1", 1, input_tokens=60_000, output_tokens=40_000),
            _record("r2", 2, input_tokens=30_000, output_tokens=20_000),
        ]
        stats = compute_stats(records, pr_states={})
        assert stats.input_tokens == 90_000
        assert stats.output_tokens == 60_000
        assert stats.total_tokens == 150_000

    def test_cache_token_totals(self):
        records = [
            _record("r1", 1, cache_creation_tokens=2_000, cache_read_tokens=8_000),
            _record("r2", 2, cache_creation_tokens=1_000, cache_read_tokens=4_000),
        ]
        stats = compute_stats(records, pr_states={})
        assert stats.cache_creation_tokens == 3_000
        assert stats.cache_read_tokens == 12_000

    def test_cost_per_merged_pr_formula(self):
        """Window spend (incl. failed runs) / distinct merged PRs."""
        url_a = "https://github.com/acme/widgets/pull/1"
        url_b = "https://github.com/acme/widgets/pull/2"
        records = [
            _record("r1", 1, status="success", cost=4.0, pr_url=url_a),
            _record("r2", 1, status="success", cost=2.0, pr_url=url_a),
            _record("r3", 2, status="success", cost=1.0, pr_url=url_b),
            _record("r4", 3, status="failure", cost=3.0),  # no PR, spend counts
        ]
        stats = compute_stats(records, pr_states={url_a: "MERGED", url_b: "OPEN"})
        assert stats.prs_opened == 2  # distinct URLs; r4 excluded (no pr_url)
        assert stats.prs_merged == 1
        assert stats.cost_per_merged_pr == 10.0  # 10.0 spend / 1 merged

    def test_zero_merged_prs_yields_none_not_division_error(self):
        url = "https://github.com/acme/widgets/pull/1"
        records = [_record("r1", 1, cost=5.0, pr_url=url)]
        stats = compute_stats(records, pr_states={url: "OPEN"})
        assert stats.prs_merged == 0
        assert stats.cost_per_merged_pr is None

    def test_unreachable_github_yields_none_outcomes(self):
        records = [_record("r1", 1, cost=5.0)]
        stats = compute_stats(records, pr_states=None)
        assert stats.prs_merged is None
        assert stats.cost_per_merged_pr is None

    def test_median_cost(self):
        records = [
            _record(f"r{i}", i, cost=c)
            for i, c in enumerate([0.4, 1.0, 2.0, 3.0, 4.0, 6.0], start=1)
        ]
        stats = compute_stats(records, pr_states={})
        assert stats.median_cost == 2.5

    def test_median_cost_is_none_for_empty_window(self):
        stats = compute_stats([], pr_states={})
        assert stats.median_cost is None
        assert stats.total_spend == 0.0
        assert stats.run_count == 0

    def test_top_issues_aggregates_runs_capped_at_five(self):
        records = [
            _record("r1", 1, cost=1.0, title="One"),
            _record("r2", 1, cost=2.5, title="One"),  # same issue: 3.5 total
            _record("r3", 2, cost=3.0, title="Two"),
            _record("r4", 3, cost=0.2, title="Three"),
            _record("r5", 4, cost=0.3, title="Four"),
            _record("r6", 5, cost=0.4, title="Five"),
            _record("r7", 6, cost=0.1, title="Six"),  # cheapest -> cut
        ]
        stats = compute_stats(records, pr_states={})
        assert len(stats.top_issues) == 5
        assert stats.top_issues[0].issue_num == 1
        assert stats.top_issues[0].cost_usd == 3.5
        assert stats.top_issues[1].issue_num == 2
        assert all(i.issue_num != 6 for i in stats.top_issues)

    def test_top_issues_keyed_on_item_id_for_linear(self):
        # Linear rows share the 0-sentinel issue_num; aggregation must key on
        # item_id so distinct items don't collapse into a single row.
        records = [
            _record("r1", 0, item_id="SFD-12", cost=1.0, title="A"),
            _record("r2", 0, item_id="SFD-12", cost=2.0, title="A"),  # 3.0 total
            _record("r3", 0, item_id="SFD-13", cost=4.0, title="B"),
        ]
        stats = compute_stats(records, pr_states={})
        by_item = {i.item_id: i for i in stats.top_issues}
        assert set(by_item) == {"SFD-12", "SFD-13"}
        assert by_item["SFD-12"].cost_usd == 3.0

    def test_top_issues_renders_item_ref_semantics(self):
        records = [
            _record("r1", 0, item_id="SFD-12", cost=2.0, title="Linear"),
            _record("r2", 7, item_id="7", cost=1.0, title="GitHub"),
        ]
        from kew.report import _render_top_issues

        stats = compute_stats(records, pr_states={})
        rows = _render_top_issues(stats.top_issues)
        body = "\n".join(rows)
        assert "| SFD-12 | Linear" in body  # bare, no leading '#'
        assert "| #7 | GitHub" in body

    def test_group_stats_by_model_and_runner_exclude_pending(self):
        records = [
            _record(
                "r1",
                1,
                status="success",
                cost=4.0,
                model="claude-sonnet-4-6",
                runner_name="claude-code",
            ),
            _record(
                "r2",
                2,
                status="failure",
                cost=3.0,
                model="claude-sonnet-4-6",
                runner_name="claude-code",
            ),
            _record(
                "r3",
                3,
                status="success",
                cost=1.0,
                model="claude-haiku-4-5",
                runner_name="opencode",
            ),
            _record(
                "r4",
                4,
                status="pending",
                cost=0.5,
                model="claude-haiku-4-5",
                runner_name="opencode",
            ),
        ]
        stats = compute_stats(records, pr_states={})
        sonnet = stats.by_model["claude-sonnet-4-6"]
        assert (sonnet.runs, sonnet.success) == (2, 1)
        assert sonnet.success_rate == 0.5
        assert sonnet.avg_cost == 3.5
        haiku = stats.by_model["claude-haiku-4-5"]
        assert haiku.runs == 1  # pending excluded
        cc = stats.by_runner["claude-code"]
        assert (cc.runs, cc.success) == (2, 1)
        assert stats.by_runner["opencode"].runs == 1

    def test_held_lists_budget_exceeded_runs(self):
        records = [
            _record("r1", 1, status="success", cost=1.0),
            _record(
                "r2", 9, status="budget_exceeded", cost=6.0, title="Migrate DB layer"
            ),
        ]
        stats = compute_stats(records, pr_states={})
        assert len(stats.held) == 1
        assert stats.held[0].issue_num == 9
        assert stats.held[0].cost_usd == 6.0

    def test_held_aggregates_multiple_runs_per_issue(self):
        """One issue hit multiple times shows a single summed held entry."""
        records = [
            _record(
                "r1", 9, status="budget_exceeded", cost=6.0, title="Migrate DB layer"
            ),
            _record(
                "r2", 9, status="budget_exceeded", cost=4.5, title="Migrate DB layer"
            ),
            _record("r3", 4, status="budget_exceeded", cost=2.0, title="Rewrite auth"),
        ]
        stats = compute_stats(records, pr_states={})
        assert len(stats.held) == 2  # one entry per distinct issue
        held_by_issue = {h.issue_num: h for h in stats.held}
        assert held_by_issue[9].cost_usd == 10.5  # 6.0 + 4.5 summed
        assert held_by_issue[4].cost_usd == 2.0
        assert [h.issue_num for h in stats.held] == [4, 9]  # sorted by issue

    def test_budget_stopped_counted_and_held(self):
        records = [
            _record("r1", 1, status="success", cost=1.0),
            _record("r2", 2, status="budget_stopped", cost=8.0, title="Heavy task"),
            _record("r3", 3, status="budget_exceeded", cost=5.0, title="Over limit"),
        ]
        stats = compute_stats(records, pr_states={})
        assert stats.budget_stopped_count == 1
        assert stats.budget_exceeded_count == 1
        held_issues = {h.issue_num for h in stats.held}
        assert held_issues == {2, 3}


GOLDEN_DIR = Path(__file__).parent / "golden"

_NOW = datetime(2026, 6, 9, 12, 0, tzinfo=UTC)

_PR_STATES = {
    "https://github.com/acme/widgets/pull/201": "MERGED",
    "https://github.com/acme/widgets/pull/202": "MERGED",
    "https://github.com/acme/widgets/pull/205": "OPEN",
}


def _seed_window_records() -> list[RunRecord]:
    """A typical week: mixed statuses, one killed run, one multi-PR mix."""
    return [
        _record(
            "run-1",
            101,
            title="Add login form",
            status="success",
            cost=4.00,
            input_tokens=60_000,
            output_tokens=40_000,
            model="claude-sonnet-4-6",
            runner_name="claude-code",
            pr_url="https://github.com/acme/widgets/pull/201",
            started_at=datetime(2026, 6, 3, 9, 0, tzinfo=UTC),
        ),
        _record(
            "run-2",
            102,
            title="Fix flaky test",
            status="success",
            cost=2.00,
            input_tokens=30_000,
            output_tokens=20_000,
            model="claude-haiku-4-5",
            runner_name="claude-code",
            pr_url="https://github.com/acme/widgets/pull/202",
            started_at=datetime(2026, 6, 4, 9, 0, tzinfo=UTC),
        ),
        _record(
            "run-3",
            103,
            title="Refactor parser",
            status="failure",
            cost=3.00,
            input_tokens=50_000,
            output_tokens=30_000,
            model="claude-sonnet-4-6",
            runner_name="claude-code",
            error_category="permanent",
            started_at=datetime(2026, 6, 5, 9, 0, tzinfo=UTC),
        ),
        _record(
            "run-4",
            104,
            title="Migrate DB layer",
            status="budget_exceeded",
            cost=6.00,
            input_tokens=70_000,
            output_tokens=50_000,
            model="claude-opus-4-6",
            runner_name="claude-code",
            error_category="budget_exceeded",
            started_at=datetime(2026, 6, 6, 9, 0, tzinfo=UTC),
        ),
        _record(
            "run-5",
            105,
            title="Update docs",
            status="success",
            cost=1.00,
            input_tokens=12_000,
            output_tokens=8_000,
            model="claude-haiku-4-5",
            runner_name="opencode",
            pr_url="https://github.com/acme/widgets/pull/205",
            started_at=datetime(2026, 6, 7, 9, 0, tzinfo=UTC),
        ),
        _record(
            "run-6",
            106,
            title="Spike: cache layer",
            status="failure",
            cost=0.40,
            input_tokens=6_000,
            output_tokens=4_000,
            model="claude-sonnet-4-6",
            runner_name="claude-code",
            error_category="stale_pending",
            started_at=datetime(2026, 6, 8, 9, 0, tzinfo=UTC),
        ),
    ]


class TestRenderReportGolden:
    def test_typical_week_matches_golden(self):
        content = render_report(
            _seed_window_records(),
            repo="acme/widgets",
            days=7,
            now=_NOW,
            budget=BudgetConfig(),
            pr_states=dict(_PR_STATES),
        )
        expected = (GOLDEN_DIR / "report_typical.md").read_text(encoding="utf-8")
        assert content == expected

    def test_empty_window_matches_golden(self):
        content = render_report(
            [],
            repo="acme/widgets",
            days=7,
            now=_NOW,
            budget=BudgetConfig(),
            pr_states={},
        )
        expected = (GOLDEN_DIR / "report_empty.md").read_text(encoding="utf-8")
        assert content == expected


class TestRenderReportEdgeCases:
    def _render(self, **overrides):
        kwargs = {
            "repo": "acme/widgets",
            "days": 7,
            "now": _NOW,
            "budget": BudgetConfig(),
            "pr_states": dict(_PR_STATES),
        }
        kwargs.update(overrides)
        return render_report(_seed_window_records(), **kwargs)

    def test_all_seven_sections_present(self):
        content = self._render()
        for marker in (
            "# kew spend report — acme/widgets",  # 1. header
            "## Totals",  # 2. totals incl. caps
            "## Outcomes",  # 3. outcomes
            "## Top 5 most expensive issues",  # 4. top-5
            "## By model",  # 5a. breakdown
            "## By runner",  # 5b. breakdown
            "## Held / blocked",  # 6. held/blocked
            "*Cost per merged PR = total spend in window",  # 7. footer formula
        ):
            assert marker in content

    def test_zero_merged_prs_renders_na(self):
        states = {url: "OPEN" for url in _PR_STATES}
        content = self._render(pr_states=states)
        assert "- **PRs merged:** 0" in content
        assert "- **Cost per merged PR:** n/a" in content

    def test_github_unreachable_degrades_outcomes_only(self):
        content = self._render(pr_states=None)
        assert "unavailable (GitHub unreachable)" in content
        assert "- **PRs opened:**" not in content
        # every other section still renders
        assert "- **Total spend:** $16.40" in content
        assert "## Top 5 most expensive issues" in content
        assert "| claude-code | 5 | 40.0% | $3.08 |" in content

    def test_caps_unconfigured_omits_caps_comparison(self):
        budget = BudgetConfig(per_issue_usd=0.0, daily_usd=0.0, weekly_usd=0.0)
        content = self._render(budget=budget)
        assert "Daily cap" not in content
        assert "Weekly cap" not in content
        assert "- **Total spend:** $16.40" in content

    def test_pipe_in_title_is_escaped_in_table(self):
        records = [_record("r1", 7, title="a | b", cost=1.0)]
        content = render_report(
            records,
            repo="acme/widgets",
            days=7,
            now=_NOW,
            budget=BudgetConfig(),
            pr_states={},
        )
        assert "a \\| b" in content


class TestBuildReport:
    def _config(self, tmp_path: Path) -> DispatchConfig:
        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        config.project.repo = "acme/widgets"
        return config

    def _seed_store(self, config: DispatchConfig, records) -> None:
        db_dir = Path(config.logging.dir)
        db_dir.mkdir(parents=True, exist_ok=True)
        with SqliteRunStore(str(db_dir / "dispatch.db")) as store:
            for record in records:
                store.save(record)

    def test_window_filters_by_started_at(self, tmp_path):
        config = self._config(tmp_path)
        self._seed_store(
            config,
            [
                _record(
                    "run-in",
                    1,
                    title="In window",
                    cost=2.0,
                    started_at=_NOW - timedelta(days=3),
                ),
                _record(
                    "run-out",
                    2,
                    title="Too old",
                    cost=9.0,
                    started_at=_NOW - timedelta(days=10),
                ),
            ],
        )
        with patch("kew.report.sync_outcomes", return_value=SyncResult()):
            content = build_report(config=config, days=7, now=_NOW)
        assert "- **Total spend:** $2.00" in content
        assert "Too old" not in content

    def test_boundary_run_exactly_at_since_is_included(self, tmp_path):
        config = self._config(tmp_path)
        self._seed_store(
            config,
            [
                _record(
                    "run-b",
                    3,
                    title="Boundary",
                    cost=1.0,
                    started_at=_NOW - timedelta(days=7),
                ),
            ],
        )
        with patch("kew.report.sync_outcomes", return_value=SyncResult()):
            content = build_report(config=config, days=7, now=_NOW)
        assert "- **Runs:** 1 (" in content

    def test_missing_db_renders_zero_activity_report(self, tmp_path):
        config = self._config(tmp_path)  # no dispatch.db created
        with patch("kew.report.sync_outcomes", return_value=SyncResult()):
            content = build_report(config=config, days=7, now=_NOW)
        assert "- **Total spend:** $0.00" in content
        assert "- **Cost per merged PR:** n/a" in content

    def _seed_store_with_outcomes(
        self, config: DispatchConfig, records, pr_states_map
    ) -> None:
        """Seed records and persist pr_state outcomes for records with a pr_url."""
        db_dir = Path(config.logging.dir)
        db_dir.mkdir(parents=True, exist_ok=True)
        with SqliteRunStore(str(db_dir / "dispatch.db")) as store:
            for rec in records:
                store.save(rec)
                if rec.pr_url and rec.pr_url in pr_states_map:
                    store.set_outcome(
                        rec.run_id,
                        pr_url=rec.pr_url,
                        pr_state=pr_states_map[rec.pr_url],
                        pr_merged_at=None,
                        review_cycles=None,
                        diff_additions=None,
                        diff_deletions=None,
                        diff_files=None,
                    )

    def test_persisted_pr_states_used_for_cost_per_merged_pr(self, tmp_path):
        """Runs with persisted MERGED outcomes contribute to cost-per-merged-PR."""
        config = self._config(tmp_path)
        self._seed_store_with_outcomes(config, _seed_window_records(), _PR_STATES)
        with patch("kew.report.sync_outcomes", return_value=SyncResult()):
            content = build_report(config=config, days=7, now=_NOW)
        assert "- **Cost per merged PR:** $8.20" in content

    def test_repo_falls_back_to_pr_url_then_placeholder(self, tmp_path):
        config = self._config(tmp_path)
        config.project.repo = ""
        self._seed_store_with_outcomes(config, _seed_window_records(), _PR_STATES)
        with patch("kew.report.sync_outcomes", return_value=SyncResult()):
            content = build_report(config=config, days=7, now=_NOW)
        assert content.startswith("# kew spend report — acme/widgets")

    def test_repo_placeholder_when_nothing_to_derive_from(self, tmp_path):
        config = self._config(tmp_path)
        config.project.repo = ""
        with patch("kew.report.sync_outcomes", return_value=SyncResult()):
            content = build_report(config=config, days=7, now=_NOW)
        assert content.startswith("# kew spend report — (unknown repo)")


cli_runner = CliRunner()


def _cli_seed_records() -> list[RunRecord]:
    """Seeded relative to the real clock — the CLI uses datetime.now()."""
    now = datetime.now(UTC)
    return [
        _record(
            "cli-1",
            101,
            title="Add login form",
            status="success",
            cost=4.00,
            pr_url="https://github.com/acme/widgets/pull/201",
            started_at=now - timedelta(days=1),
        ),
        _record(
            "cli-2",
            102,
            title="Fix flaky test",
            status="success",
            cost=2.00,
            pr_url="https://github.com/acme/widgets/pull/202",
            started_at=now - timedelta(days=2),
        ),
        _record(
            "cli-3",
            105,
            title="Update docs",
            status="success",
            cost=1.00,
            pr_url="https://github.com/acme/widgets/pull/205",
            started_at=now - timedelta(days=3),
        ),
        _record(
            "cli-4",
            104,
            title="Migrate DB layer",
            status="budget_exceeded",
            cost=6.00,
            started_at=now - timedelta(days=4),
        ),
    ]


class TestReportCommand:
    def _config(self, tmp_path: Path) -> DispatchConfig:
        config = DispatchConfig()
        config.logging.dir = str(tmp_path / "logs")
        config.project.repo = "acme/widgets"
        return config

    def _seed(self, config: DispatchConfig, records, pr_states_map=None) -> None:
        """Seed records; optionally persist pr_state outcomes for pr_url matches."""
        db_dir = Path(config.logging.dir)
        db_dir.mkdir(parents=True, exist_ok=True)
        with SqliteRunStore(str(db_dir / "dispatch.db")) as store:
            for record in records:
                store.save(record)
                if pr_states_map and record.pr_url and record.pr_url in pr_states_map:
                    store.set_outcome(
                        record.run_id,
                        pr_url=record.pr_url,
                        pr_state=pr_states_map[record.pr_url],
                        pr_merged_at=None,
                        review_cycles=None,
                        diff_additions=None,
                        diff_deletions=None,
                        diff_files=None,
                    )

    @patch("kew.cli.preflight_tools")
    @patch("kew.cli.load_config")
    def test_default_window_renders_report_with_persisted_outcomes(
        self, mock_config: MagicMock, mock_preflight: MagicMock, tmp_path: Path
    ):
        # Build per-record pr_states from _cli_seed_records pr_urls
        cli_pr_states = {
            "https://github.com/acme/widgets/pull/201": "MERGED",
            "https://github.com/acme/widgets/pull/202": "MERGED",
            "https://github.com/acme/widgets/pull/205": "OPEN",
        }
        config = self._config(tmp_path)
        mock_config.return_value = config
        self._seed(config, _cli_seed_records(), cli_pr_states)
        with patch("kew.report.sync_outcomes", return_value=SyncResult()):
            result = cli_runner.invoke(app, ["report"])
        assert result.exit_code == 0
        assert "# kew spend report — acme/widgets" in result.output
        assert "(7 days)" in result.output  # default --days
        # spend 13.00 / 2 merged PRs (201, 202)
        assert "- **Cost per merged PR:** $6.50" in result.output

    @patch("kew.cli.preflight_tools")
    @patch("kew.cli.load_config")
    def test_round17_github_unreachable_exits_two_with_degraded_outcomes(
        self, mock_config: MagicMock, mock_preflight: MagicMock, tmp_path: Path
    ):
        # Query failures preserve spend output while making availability explicit.
        config = self._config(tmp_path)
        mock_config.return_value = config
        self._seed(config, _cli_seed_records())
        with patch(
            "kew.report.sync_outcomes", return_value=SyncResult(query_failures=1)
        ):
            result = cli_runner.invoke(app, ["report"])
        assert result.exit_code == 2
        assert "- **Total spend:** $13.00" in result.output

    @patch("kew.cli.preflight_tools")
    @patch("kew.cli.load_config")
    def test_days_flag_filters_window(
        self, mock_config: MagicMock, mock_preflight: MagicMock, tmp_path: Path
    ):
        config = self._config(tmp_path)
        mock_config.return_value = config
        now = datetime.now(UTC)
        self._seed(
            config,
            [
                _record(
                    "cli-r",
                    1,
                    title="Recent",
                    cost=2.0,
                    started_at=now - timedelta(days=2),
                ),
                _record(
                    "cli-o",
                    2,
                    title="Old",
                    cost=5.0,
                    started_at=now - timedelta(days=9),
                ),
            ],
        )
        with patch("kew.report.sync_outcomes", return_value=SyncResult()):
            result = cli_runner.invoke(app, ["report", "--days", "5"])
        assert result.exit_code == 0
        assert "(5 days)" in result.output
        assert "Recent" in result.output
        assert "Old" not in result.output
        assert "- **Total spend:** $2.00" in result.output

    @patch("kew.cli.preflight_tools")
    @patch("kew.cli.load_config")
    def test_days_must_be_positive(
        self, mock_config: MagicMock, mock_preflight: MagicMock, tmp_path: Path
    ):
        mock_config.return_value = self._config(tmp_path)
        result = cli_runner.invoke(app, ["report", "--days", "0"])
        assert result.exit_code == 2

    @patch("kew.cli.preflight_tools")
    @patch("kew.cli.load_config")
    def test_output_flag_writes_identical_markdown(
        self, mock_config: MagicMock, mock_preflight: MagicMock, tmp_path: Path
    ):
        config = self._config(tmp_path)
        mock_config.return_value = config
        self._seed(config, _cli_seed_records())
        out_path = tmp_path / "report.md"
        frozen = datetime(2026, 6, 9, 12, 0, tzinfo=UTC)
        with (
            patch("kew.report.sync_outcomes", return_value=SyncResult()),
            patch("kew.report.datetime") as mock_dt,
        ):
            mock_dt.now.return_value = frozen
            result_stdout = cli_runner.invoke(app, ["report"])
            result_file = cli_runner.invoke(app, ["report", "--output", str(out_path)])
        assert result_stdout.exit_code == 0
        assert result_file.exit_code == 0
        file_content = out_path.read_text(encoding="utf-8")
        # typer.echo appends one newline to the identical content
        assert result_stdout.output == file_content + "\n"
        assert file_content.startswith("# kew spend report — acme/widgets")


def test_build_report_reads_persisted_pr_state_no_live_poll(monkeypatch, tmp_path):
    from kew.config import DispatchConfig
    from kew.report import build_report
    from kew.run_store import SqliteRunStore

    cfg = DispatchConfig()
    cfg.logging.dir = str(tmp_path / "logs")
    (tmp_path / "logs").mkdir(parents=True)
    pr_url = "https://github.com/o/r/pull/3"
    db = tmp_path / "logs" / "dispatch.db"
    with SqliteRunStore(str(db)) as store:
        rec = _record("run-p1", 3, status="success", pr_url=pr_url)
        store.save(rec)
        store.set_outcome(
            rec.run_id,
            pr_url=pr_url,
            pr_state="MERGED",
            pr_merged_at=None,
            review_cycles=1,
            diff_additions=1,
            diff_deletions=0,
            diff_files=1,
        )

    def _boom(_urls):
        raise AssertionError("resolve_pr_states (live poll) must not be called")

    monkeypatch.setattr("kew.report.resolve_pr_states", _boom)
    monkeypatch.setattr(
        "kew.report.sync_outcomes", lambda store, config: SyncResult()
    )  # offline

    out = build_report(config=cfg, days=30)
    # 1 merged PR reflected from persisted pr_state — match the exact rendered line
    assert "- **PRs merged:** 1" in out


def test_status_counts_counts_killed_status_directly():
    from kew.report import compute_stats

    records = [
        _record("k1", 1, status="killed"),  # operator kill
        _record(
            "c1", 2, status="failure", error_category="stale_pending"
        ),  # crashed cleanup
        _record("f1", 3, status="failure"),  # ordinary failure
        _record("s1", 4, status="success"),
    ]
    stats = compute_stats(records, pr_states={})
    assert stats.killed_count == 2  # killed + stale_pending failure
    assert stats.failure_count == 1  # ordinary failure only
    assert stats.success_count == 1


def _seed_approval(
    store, *, issue_num, event, estimate=10.0, ts="2026-06-24T10:00:00Z"
):
    store.append_audit_event(
        timestamp=ts,
        event=event,
        issue_num=issue_num,
        payload={
            "estimate_usd": estimate,
            "basis": "median-of-10",
            "threshold_usd": 5.0,
        },
    )


def test_open_approval_holds_returns_open_holds_with_estimate(tmp_path):
    from kew.report import open_approval_holds
    from kew.run_store import SqliteRunStore

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _seed_approval(store, issue_num=19, event="approval_hold", estimate=14.0)
        holds = open_approval_holds(store)
    assert [h.issue_num for h in holds] == [19]
    assert holds[0].estimate_usd == 14.0
    assert holds[0].basis == "median-of-10"


def test_open_approval_holds_excludes_resolved(tmp_path):
    from kew.report import open_approval_holds
    from kew.run_store import SqliteRunStore

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        # issue 19: held then granted (granted is newer = higher id) -> resolved
        _seed_approval(
            store, issue_num=19, event="approval_hold", ts="2026-06-24T10:00:00Z"
        )
        _seed_approval(
            store, issue_num=19, event="approval_granted", ts="2026-06-24T11:00:00Z"
        )
        # issue 27: still held
        _seed_approval(store, issue_num=27, event="approval_hold", estimate=9.0)
        holds = open_approval_holds(store)
    assert [h.issue_num for h in holds] == [27]  # 19 resolved, excluded


def test_list_audit_events_event_prefix_filters_by_prefix(tmp_path):
    from kew.run_store import SqliteRunStore

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        store.append_audit_event(
            timestamp="2026-06-24T10:00:00Z",
            event="dispatch_completed",
            issue_num=1,
        )
        _seed_approval(store, issue_num=2, event="approval_hold")
        store.append_audit_event(
            timestamp="2026-06-24T10:30:00Z",
            event="approvalXhold",
            issue_num=99,
        )
        _seed_approval(
            store, issue_num=3, event="approval_granted", ts="2026-06-24T11:00:00Z"
        )
        all_events = store.list_audit_events(limit=None)
        approval_events = store.list_audit_events(limit=None, event_prefix="approval_")
    assert len(all_events) == 4
    assert len(approval_events) == 2
    assert all(e.event.startswith("approval_") for e in approval_events)


def test_open_approval_holds_scoped_to_issue_nums(tmp_path):
    """The scope is a narrowing, not a second rule: inside it the latest-event
    rule still decides. A caller that only cares about one epic's nodes must
    not pay to parse every approval event on the host (#2129)."""
    from kew.report import open_approval_holds
    from kew.run_store import SqliteRunStore

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _seed_approval(store, issue_num=19, event="approval_hold")
        _seed_approval(store, issue_num=27, event="approval_hold")
        _seed_approval(
            store, issue_num=31, event="approval_hold", ts="2026-06-24T10:00:00Z"
        )
        _seed_approval(
            store, issue_num=31, event="approval_granted", ts="2026-06-24T11:00:00Z"
        )
        scoped = open_approval_holds(store, issue_nums=[27, 31])
        assert [h.issue_num for h in scoped] == [27]  # 19 out of scope, 31 resolved
        assert open_approval_holds(store, issue_nums=[]) == []


def test_open_approval_holds_pushes_the_scope_into_the_audit_read(tmp_path):
    """Scoping in SQL, not in Python — filtering after the read still parses
    every approval payload on the host, which is the cost being removed."""
    from unittest.mock import patch

    from kew.report import open_approval_holds
    from kew.run_store import SqliteRunStore

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        _seed_approval(store, issue_num=27, event="approval_hold")
        with patch.object(
            store, "list_audit_events", wraps=store.list_audit_events
        ) as mock_list:
            open_approval_holds(store, issue_nums=[27])
            mock_list.assert_called_once_with(
                limit=None, event_prefix="approval_", issue_nums=[27]
            )


def test_open_approval_holds_uses_event_prefix(tmp_path):
    """open_approval_holds uses event_prefix so it doesn't scan all events."""
    from unittest.mock import patch

    from kew.report import open_approval_holds
    from kew.run_store import SqliteRunStore

    with SqliteRunStore(str(tmp_path / "d.db")) as store:
        store.append_audit_event(
            timestamp="2026-06-24T09:00:00Z",
            event="dispatch_completed",
            issue_num=1,
        )
        _seed_approval(store, issue_num=2, event="approval_hold")
        with patch.object(
            store, "list_audit_events", wraps=store.list_audit_events
        ) as mock_list:
            holds = open_approval_holds(store)
            mock_list.assert_called_once_with(
                limit=None, event_prefix="approval_", issue_nums=None
            )
    assert [h.issue_num for h in holds] == [2]


def test_render_held_renders_budget_and_approval_holds():
    from kew.report import ApprovalHold, IssueCost, _render_held

    budget = [
        IssueCost(
            issue_num=12,
            title="fix thing",
            cost_usd=8.2,
            status="budget_exceeded",
            pr_url=None,
        )
    ]
    holds = [ApprovalHold(issue_num=19, estimate_usd=14.0, basis="median-of-10")]
    lines = _render_held(budget, holds)
    body = "\n".join(lines)
    assert "#12 fix thing — budget exceeded ($8.20)" in body
    assert "#19 — needs approval (~$14.00 estimated)" in body


def test_render_held_none_only_when_both_empty():
    from kew.report import _render_held

    assert _render_held([], []) == ["None."]


def test_build_report_surfaces_approval_hold(monkeypatch, tmp_path):
    from kew.config import DispatchConfig
    from kew.report import build_report
    from kew.run_store import SqliteRunStore

    cfg = DispatchConfig()
    cfg.logging.dir = str(tmp_path / "logs")
    cfg.project.repo = "o/r"
    (tmp_path / "logs").mkdir(parents=True)
    with SqliteRunStore(str(tmp_path / "logs" / "dispatch.db")) as store:
        _seed_approval(store, issue_num=19, event="approval_hold", estimate=14.0)
    monkeypatch.setattr(
        "kew.report.sync_outcomes", lambda store, config: SyncResult()
    )  # offline
    out = build_report(config=cfg, days=30)
    assert "#19 — needs approval (~$14.00 estimated)" in out


def test_report_includes_reported_model_breakdown():
    from kew.report import _group_breakdowns

    records = [
        _record("a", 1, status="success", model="claude-sonnet-4-6", cost=1.0),
        _record("b", 2, status="success", model="claude-sonnet-4-6", cost=1.0),
    ]
    records[0].reported_model = "anthropic/claude-sonnet-4-6"
    records[1].reported_model = None  # opencode reports nothing
    by_model, by_runner, by_reported_model = _group_breakdowns(records)
    assert by_reported_model["anthropic/claude-sonnet-4-6"].runs == 1
    assert "unknown" not in by_reported_model  # null reported models are skipped


def _iu(model, cost, tokens, **over):
    from kew.run_store import InteractiveUsage

    base = dict(
        runner="claude-code",
        model=model,
        observed_at="2026-06-26T12:00:00+00:00",
        input_tokens=tokens,
        output_tokens=0,
        cache_creation_tokens=0,
        cache_read_tokens=0,
        cost_usd=cost,
        session_id="s",
        ingest_source="file:x",
        dedup_key=f"{model}-{cost}-{tokens}",
    )
    base.update(over)
    return InteractiveUsage(**base)


def test_render_includes_interactive_section_when_present():
    from datetime import UTC, datetime

    from kew.config import BudgetConfig
    from kew.report import render_report

    md = render_report(
        [],
        repo="o/r",
        days=7,
        now=datetime(2026, 6, 26, tzinfo=UTC),
        budget=BudgetConfig(),
        pr_states={},
        interactive=[_iu("claude-opus-4-8", 3.05, 840_000)],
    )
    assert "## Interactive (Claude Code)" in md
    assert "claude-opus-4-8" in md


def test_render_interactive_only_window_shows_totals_line():
    # An interactive-only window (no dispatched runs) still gets a totals line,
    # showing Dispatched total: $0.00.
    from datetime import UTC, datetime

    from kew.config import BudgetConfig
    from kew.report import render_report

    md = render_report(
        [],
        repo="o/r",
        days=7,
        now=datetime(2026, 6, 26, tzinfo=UTC),
        budget=BudgetConfig(),
        pr_states={},
        interactive=[_iu("claude-opus-4-8", 3.05, 840_000)],
    )
    assert "Interactive total: $3.05" in md
    assert "Dispatched total: $0.00" in md
    assert "Combined: $3.05" in md


def test_render_omits_interactive_section_when_empty():
    from datetime import UTC, datetime

    from kew.config import BudgetConfig
    from kew.report import render_report

    md = render_report(
        [],
        repo="o/r",
        days=7,
        now=datetime(2026, 6, 26, tzinfo=UTC),
        budget=BudgetConfig(),
        pr_states={},
        interactive=[],
    )
    assert "Interactive (Claude Code)" not in md  # no empty section, no header


def test_render_interactive_splits_by_query_source():
    from datetime import UTC, datetime

    from kew.config import BudgetConfig
    from kew.report import render_report

    md = render_report(
        [],
        repo="o/r",
        days=7,
        now=datetime(2026, 6, 26, tzinfo=UTC),
        budget=BudgetConfig(),
        pr_states={},
        interactive=[
            _iu("claude-opus-4-8", 3.00, 1000, query_source="main", dedup_key="m1"),
            _iu(
                "claude-opus-4-8",
                1.50,
                500,
                query_source="subagent",
                agent_name="custom",
                dedup_key="s1",
            ),
            _iu("claude-opus-4-8", 0.50, 200, query_source="auxiliary", dedup_key="a1"),
        ],
    )
    assert "### Main" in md
    assert "### Subagent" in md
    assert "### Auxiliary" in md
    assert "Interactive total: $5.00" in md


def test_render_interactive_single_nonmain_shows_heading():
    from datetime import UTC, datetime

    from kew.config import BudgetConfig
    from kew.report import render_report

    md = render_report(
        [],
        repo="o/r",
        days=7,
        now=datetime(2026, 6, 26, tzinfo=UTC),
        budget=BudgetConfig(),
        pr_states={},
        interactive=[
            _iu(
                "claude-opus-4-8",
                1.50,
                500,
                query_source="subagent",
                agent_name="custom",
                dedup_key="s1",
            ),
        ],
    )
    assert "### Subagent" in md
    assert "Interactive total: $1.50" in md


def test_interactive_breakdown_buckets_null_as_main():
    from kew.report import _interactive_breakdown

    rows = [
        _iu("claude-opus-4-8", 1.0, 100, query_source=None, dedup_key="n1"),
        _iu("claude-opus-4-8", 2.0, 200, query_source="main", dedup_key="n2"),
    ]
    by_source, total = _interactive_breakdown(rows)
    assert "main" in by_source
    assert "subagent" not in by_source
    main_models = by_source["main"]
    assert main_models["claude-opus-4-8"].total_cost == 3.0
    assert total == 3.0


def test_interactive_total_reconciles():
    from kew.report import _interactive_breakdown

    rows = [
        _iu("claude-opus-4-8", 3.0, 1000, query_source="main", dedup_key="m1"),
        _iu("claude-opus-4-8", 1.5, 500, query_source="subagent", dedup_key="s1"),
        _iu("claude-opus-4-8", 0.5, 200, query_source="auxiliary", dedup_key="a1"),
    ]
    by_source, total = _interactive_breakdown(rows)
    source_sum = sum(
        g.total_cost for models in by_source.values() for g in models.values()
    )
    assert abs(source_sum - total) < 1e-9
    assert abs(total - 5.0) < 1e-9


def test_ingest_cap_alert_fires_over_threshold(tmp_path):
    from datetime import UTC, datetime

    from kew.config import DispatchConfig, IngestConfig, LoggingConfig
    from kew.report import ingest_cap_alert
    from kew.run_store import SqliteRunStore

    logs = tmp_path / "logs"
    logs.mkdir()
    with SqliteRunStore(str(logs / "dispatch.db")) as store:
        store.record_interactive_usage(_iu("claude-opus-4-8", 5.0, 1000))

    config = DispatchConfig(
        logging=LoggingConfig(dir=str(logs)),
        ingest=IngestConfig(cap_usd=3.0),
    )
    event = ingest_cap_alert(config, days=7, now=datetime(2026, 6, 27, tzinfo=UTC))
    assert event is not None
    assert "alert" in event.message.lower()
    assert "not" in event.message.lower()  # "not enforce/enforced"
    # pure-interactive path keeps the interactive-only label
    assert "interactive usage spend" in event.message.lower()
    assert "combined" not in event.message.lower()


def test_ingest_cap_alert_silent_under_threshold(tmp_path):
    from datetime import UTC, datetime

    from kew.config import DispatchConfig, IngestConfig, LoggingConfig
    from kew.report import ingest_cap_alert
    from kew.run_store import SqliteRunStore

    logs = tmp_path / "logs"
    logs.mkdir()
    with SqliteRunStore(str(logs / "dispatch.db")) as store:
        store.record_interactive_usage(_iu("claude-opus-4-8", 1.0, 1000))
    config = DispatchConfig(
        logging=LoggingConfig(dir=str(logs)),
        ingest=IngestConfig(cap_usd=3.0),
    )
    assert (
        ingest_cap_alert(config, days=7, now=datetime(2026, 6, 27, tzinfo=UTC)) is None
    )


def test_ingest_cap_alert_combine_with_dispatch(tmp_path):
    from datetime import UTC, datetime

    from kew.config import DispatchConfig, IngestConfig, LoggingConfig
    from kew.report import ingest_cap_alert
    from kew.run_store import SqliteRunStore

    logs = tmp_path / "logs"
    logs.mkdir()
    now = datetime(2026, 6, 27, tzinfo=UTC)
    with SqliteRunStore(str(logs / "dispatch.db")) as store:
        # interactive cost: $5.00
        store.record_interactive_usage(_iu("claude-opus-4-8", 5.0, 1000))
        # dispatched run cost: $4.00, started within the 7-day window
        store.save(
            _record("disp-1", 1, cost=4.0, started_at=datetime(2026, 6, 25, tzinfo=UTC))
        )

    # combine_with_dispatch=True: 5 + 4 = 9 >= 8 → fires
    config_combined = DispatchConfig(
        logging=LoggingConfig(dir=str(logs)),
        ingest=IngestConfig(cap_usd=8.0, combine_with_dispatch=True),
    )
    event = ingest_cap_alert(config_combined, days=7, now=now)
    assert event is not None
    # the message must not mislabel combined spend as interactive-only
    assert "combined interactive + dispatched spend" in event.message.lower()

    # combine_with_dispatch=False: interactive-only 5 < 8 → silent
    config_separate = DispatchConfig(
        logging=LoggingConfig(dir=str(logs)),
        ingest=IngestConfig(cap_usd=8.0, combine_with_dispatch=False),
    )
    assert ingest_cap_alert(config_separate, days=7, now=now) is None


def test_ingest_cap_alert_off_when_cap_zero(tmp_path):
    from datetime import UTC, datetime

    from kew.config import DispatchConfig, IngestConfig, LoggingConfig
    from kew.report import ingest_cap_alert

    config = DispatchConfig(
        logging=LoggingConfig(dir=str(tmp_path)),
        ingest=IngestConfig(cap_usd=0.0),
    )
    assert (
        ingest_cap_alert(config, days=7, now=datetime(2026, 6, 27, tzinfo=UTC)) is None
    )


def test_load_window_empty_when_no_db(tmp_path):
    from datetime import UTC, datetime

    from kew.config import DispatchConfig, LoggingConfig
    from kew.report import _load_window

    config = DispatchConfig(logging=LoggingConfig(dir=str(tmp_path / "nope")))
    window = _load_window(config, datetime(2026, 1, 1, tzinfo=UTC))
    assert window.records == []
    assert window.approval_holds == []
    assert window.interactive == []


def test_load_window_reads_records(tmp_path):
    from datetime import UTC, datetime

    from kew.config import DispatchConfig, LoggingConfig
    from kew.report import _load_window
    from kew.run_store import SqliteRunStore

    logs = tmp_path / "logs"
    logs.mkdir()
    with SqliteRunStore(str(logs / "dispatch.db")) as store:
        store.save(_record("r1", 1, started_at=datetime(2026, 6, 20, tzinfo=UTC)))
    # _load_window only needs logging.dir
    config = DispatchConfig(logging=LoggingConfig(dir=str(logs)))
    window = _load_window(config, datetime(2026, 6, 1, tzinfo=UTC))
    assert [r.run_id for r in window.records] == ["r1"]


def _sc(
    run_id,
    *,
    model,
    complexity="medium",
    status="success",
    cost=1.0,
    pr_url=None,
    pr_state=None,
    review_cycles=None,
    started_at=None,
    run_kind="dispatch",
):
    from datetime import UTC, datetime

    from kew.run_store import RunRecord

    return RunRecord(
        run_id=run_id,
        issue_num=1,
        branch="b",
        status=status,  # type: ignore[arg-type]
        exit_code=0 if status == "success" else 1,
        started_at=started_at or datetime(2026, 6, 20, tzinfo=UTC),
        estimated_cost_usd=cost,
        model=model,
        complexity=complexity,
        pr_url=pr_url,
        pr_state=pr_state,
        review_cycles=review_cycles,
        run_kind=run_kind,
    )


def _row(scorecard, model):
    return next(r for r in scorecard.rows if r.model == model)


def test_scorecard_cross_model_retry_attributes_merge_to_successful_model():
    from kew.report import compute_model_scorecard

    # opus failed (no pr_url); sonnet succeeded + merged the PR
    records = [
        _sc("r1", model="opus", status="failure", cost=1.0, pr_url=None),
        _sc(
            "r2",
            model="sonnet",
            status="success",
            cost=0.5,
            pr_url="https://github.com/o/r/pull/1",
            pr_state="MERGED",
            review_cycles=1,
        ),
    ]
    sc = compute_model_scorecard(records)
    opus, sonnet = _row(sc, "opus"), _row(sc, "sonnet")
    assert (opus.runs, opus.opened, opus.merged, opus.spend) == (1, 0, 0, 1.0)
    assert (sonnet.runs, sonnet.opened, sonnet.merged, sonnet.spend) == (1, 1, 1, 0.5)


def test_scorecard_dedupes_multi_run_one_pr():
    from datetime import UTC, datetime

    from kew.report import compute_model_scorecard

    pr = "https://github.com/o/r/pull/2"
    records = [
        _sc(
            "r1",
            model="opus",
            cost=0.4,
            pr_url=pr,
            pr_state="MERGED",
            review_cycles=2,
            started_at=datetime(2026, 6, 20, 9, tzinfo=UTC),
        ),
        _sc(
            "r2",
            model="opus",
            cost=0.6,
            pr_url=pr,
            pr_state="MERGED",
            review_cycles=2,
            started_at=datetime(2026, 6, 20, 11, tzinfo=UTC),
        ),
    ]
    opus = _row(compute_model_scorecard(records), "opus")
    assert opus.runs == 2  # both runs counted for throughput/spend
    assert opus.spend == 1.0
    assert opus.opened == 1  # one distinct PR
    assert opus.merged == 1
    assert opus.review_n == 1 and opus.review_total == 2


def test_scorecard_all_failed_pr_is_invisible():
    from kew.report import compute_model_scorecard

    # a PR a human merged but whose kew runs all failed -> no pr_url anywhere
    records = [_sc("r1", model="opus", status="failure", cost=1.0, pr_url=None)]
    opus = _row(compute_model_scorecard(records), "opus")
    assert opus.opened == 0 and opus.merged == 0 and opus.runs == 1


def test_scorecard_author_is_earliest_dispatch_started_at():
    from datetime import UTC, datetime

    from kew.report import compute_model_scorecard

    pr = "https://github.com/o/r/pull/3"
    # two dispatch runs share the PR -> the one that opened it authored it (#1131)
    records = [
        _sc(
            "r1",
            model="opus",
            cost=0.5,
            pr_url=pr,
            pr_state="MERGED",
            started_at=datetime(2026, 6, 20, 8, tzinfo=UTC),
        ),
        _sc(
            "r2",
            model="sonnet",
            cost=0.5,
            pr_url=pr,
            pr_state="MERGED",
            started_at=datetime(2026, 6, 20, 12, tzinfo=UTC),
        ),
    ]
    sc = compute_model_scorecard(records)
    assert _row(sc, "opus").merged == 1
    assert _row(sc, "sonnet").merged == 0


def test_scorecard_credits_merge_to_author_not_adversarial_reviewer():
    """End-to-end over real RunRecords: an `adversarial_review` run carries the
    author's pr_url and starts later, but must not win the merge (#1131)."""
    from datetime import UTC, datetime

    from kew.report import compute_model_scorecard

    pr = "https://github.com/o/r/pull/4"
    records = [
        _sc(
            "r1",
            model="sonnet",
            cost=2.0,
            pr_url=pr,
            pr_state="MERGED",
            review_cycles=2,
            started_at=datetime(2026, 6, 20, 8, tzinfo=UTC),
        ),
        _sc(
            "r2",
            model="opus",
            cost=3.0,
            pr_url=pr,
            pr_state="MERGED",
            review_cycles=2,
            started_at=datetime(2026, 6, 20, 12, tzinfo=UTC),
            run_kind="adversarial_review",
        ),
    ]
    sc = compute_model_scorecard(records)
    sonnet, opus = _row(sc, "sonnet"), _row(sc, "opus")
    assert (sonnet.opened, sonnet.merged, sonnet.review_total) == (1, 1, 2)
    assert (opus.opened, opus.merged, opus.review_total) == (0, 0, 0)
    # spend stays where it was incurred; only the PR outcome moved
    assert (sonnet.spend, opus.spend) == (2.0, 3.0)


def test_scorecard_complexity_subrows_reconcile_and_unknown_bucket():
    from kew.report import compute_model_scorecard

    records = [
        _sc(
            "r1",
            model="opus",
            complexity="simple",
            cost=0.2,
            pr_url="https://github.com/o/r/pull/10",
            pr_state="MERGED",
        ),
        _sc(
            "r2",
            model="opus",
            complexity="complex",
            cost=0.8,
            pr_url="https://github.com/o/r/pull/11",
            pr_state="OPEN",
        ),
        _sc("r3", model="opus", complexity="", cost=0.1, pr_url=None, status="failure"),
    ]
    opus = _row(compute_model_scorecard(records), "opus")
    assert opus.runs == 3 and opus.opened == 2 and opus.merged == 1
    # sub-rows partition the model total
    assert sum(c.runs for c in opus.by_complexity.values()) == opus.runs
    assert sum(c.opened for c in opus.by_complexity.values()) == opus.opened
    assert sum(c.merged for c in opus.by_complexity.values()) == opus.merged
    assert sum(c.spend for c in opus.by_complexity.values()) == opus.spend
    assert "unknown" in opus.by_complexity  # the complexity="" run


def test_scorecard_empty_model_groups_unknown():
    from kew.report import compute_model_scorecard

    records = [_sc("r1", model="", cost=1.0)]
    rows = compute_model_scorecard(records).rows
    assert len(rows) == 1
    assert rows[0].model == "unknown"
    assert rows[0].runs == 1


def test_scorecard_reviews_mean_includes_zero_review_merges():
    from kew.report import compute_model_scorecard

    records = [
        _sc(
            "r1",
            model="opus",
            pr_url="https://github.com/o/r/pull/20",
            pr_state="MERGED",
            review_cycles=0,
        ),
        _sc(
            "r2",
            model="opus",
            pr_url="https://github.com/o/r/pull/21",
            pr_state="MERGED",
            review_cycles=2,
        ),
    ]
    opus = _row(compute_model_scorecard(records), "opus")
    assert opus.review_n == 2 and opus.review_total == 2  # mean 1.0


def test_render_scorecard_empty():
    from datetime import UTC, datetime

    from kew.report import ModelScorecard, render_model_scorecard

    md = render_model_scorecard(
        ModelScorecard(rows=[]),
        repo="o/r",
        days=7,
        now=datetime(2026, 6, 26, tzinfo=UTC),
    )
    assert "No model activity in the last 7 days." in md


def test_render_scorecard_metrics_and_flags():
    from datetime import UTC, datetime

    from kew.report import compute_model_scorecard, render_model_scorecard

    records = [
        # opus: 2 PRs opened, 1 merged, reviews 0 & 2 -> mean 1.0, spend incl. a failure
        _sc(
            "a",
            model="opus",
            complexity="simple",
            cost=1.0,
            pr_url="https://github.com/o/r/pull/1",
            pr_state="MERGED",
            review_cycles=0,
        ),
        _sc(
            "b",
            model="opus",
            complexity="simple",
            cost=2.0,
            pr_url="https://github.com/o/r/pull/2",
            pr_state="OPEN",
            review_cycles=2,
        ),
        _sc(
            "c",
            model="opus",
            complexity="simple",
            status="failure",
            cost=0.5,
            pr_url=None,
        ),
    ]
    sc = compute_model_scorecard(records)
    md = render_model_scorecard(
        sc, repo="o/r", days=30, now=datetime(2026, 6, 26, tzinfo=UTC)
    )
    assert "# Model scorecard — o/r" in md
    assert "| opus" in md  # model name renders in table
    assert (
        "| claude" not in md
    )  # model name rendered verbatim (no claude- prefix injected)
    assert "50%* (1/2)" in md  # 1 merged / 2 opened, opened<3 -> starred
    assert "$3.50" in md  # spend 3.5 / 1 merged
    assert "1.0" in md  # reviews mean
    assert "low sample (<3 PRs)" in md


def test_render_scorecard_em_dash_on_zero_denominators():
    from datetime import UTC, datetime

    from kew.report import compute_model_scorecard, render_model_scorecard

    # a model with runs but no PRs at all
    records = [_sc("a", model="opus", status="failure", cost=1.0, pr_url=None)]
    md = render_model_scorecard(
        compute_model_scorecard(records),
        repo="o/r",
        days=7,
        now=datetime(2026, 6, 26, tzinfo=UTC),
    )
    assert "| —" in md  # merge rate and $/merged render em dash, no crash, no 0%
    assert md.count("| —") >= 2  # both merge-rate and $/merged are em dashes
    assert "—*" not in md  # zero-denominator rows must never get a low-sample star


def test_render_scorecard_escapes_complexity_label():
    from kew.report import compute_model_scorecard, render_model_scorecard

    # a pipe in the complexity label must be escaped so the table can't break
    records = [
        _sc(
            "r1",
            model="opus",
            complexity="a|b",
            cost=1.0,
            pr_url="https://github.com/o/r/pull/1",
            pr_state="MERGED",
        ),
    ]
    md = render_model_scorecard(
        compute_model_scorecard(records),
        repo="o/r",
        days=7,
        now=datetime(2026, 6, 26, tzinfo=UTC),
    )
    assert "· a\\|b" in md  # escaped form present
    assert "· a|b" not in md  # no raw, table-breaking pipe


def test_build_model_scorecard_empty_no_db(tmp_path):
    from kew.config import DispatchConfig, LoggingConfig, ProjectConfig
    from kew.report import build_model_scorecard

    config = DispatchConfig(
        project=ProjectConfig(repo="o/r"),
        logging=LoggingConfig(dir=str(tmp_path / "nope")),
    )
    md = build_model_scorecard(config=config, days=7)
    assert "No model activity" in md


def test_cli_report_models_renders_scorecard(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from kew.cli import app
    from kew.run_store import SqliteRunStore

    monkeypatch.chdir(tmp_path)
    logs = tmp_path / "logs"
    logs.mkdir()
    (tmp_path / "kew.toml").write_text(
        f'[project]\nrepo = "o/r"\n[logging]\ndir = "{logs}"\n'
    )
    with SqliteRunStore(str(logs / "dispatch.db")) as store:
        store.save(
            _sc(
                "r1",
                model="opus",
                cost=1.0,
                pr_url="https://github.com/o/r/pull/1",
                pr_state="MERGED",
            )
        )
    # hermetic (no gh/git preflight); wide window so the fixed started_at never
    # falls out of range as wall-clock advances (no time-bomb).
    with patch("kew.cli.preflight_tools"), patch("kew.cli._detect_repo"):
        result = CliRunner().invoke(app, ["report", "--models", "--days", "3650"])
    assert result.exit_code == 0, result.output
    assert "Model scorecard" in result.output
    assert "opus" in result.output


def test_cli_report_models_rejects_notify(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from kew.cli import app

    monkeypatch.chdir(tmp_path)
    (tmp_path / "kew.toml").write_text('[project]\nrepo = "o/r"\n')
    with patch("kew.cli.preflight_tools"), patch("kew.cli._detect_repo"):
        result = CliRunner().invoke(app, ["report", "--models", "--notify"])
    assert result.exit_code != 0
    assert "notify" in result.output.lower()


def test_cli_report_models_rejects_notify_before_preflight(tmp_path, monkeypatch):
    # The usage error must fire BEFORE preflight_tools (which can SystemExit on a
    # machine missing gh/git) — so we get the notify usage error, not a tool error.
    from typer.testing import CliRunner

    from kew.cli import app

    monkeypatch.chdir(tmp_path)
    (tmp_path / "kew.toml").write_text('[project]\nrepo = "o/r"\n')
    with patch("kew.cli.preflight_tools", side_effect=SystemExit(1)):
        result = CliRunner().invoke(app, ["report", "--models", "--notify"])
    assert result.exit_code != 0
    assert "notify" in result.output.lower()


def _seed_export_store(logs, records):
    from kew.run_store import SqliteRunStore

    logs.mkdir(parents=True, exist_ok=True)
    with SqliteRunStore(str(logs / "dispatch.db")) as store:
        for r in records:
            store.save(r)


def test_cli_report_export_html(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from kew.cli import app

    monkeypatch.chdir(tmp_path)
    logs = tmp_path / "logs"
    (tmp_path / "kew.toml").write_text(
        f'[project]\nrepo = "o/r"\n[logging]\ndir = "{logs}"\n'
    )
    _seed_export_store(
        logs,
        [
            _sc(
                "r1",
                model="opus",
                cost=6.0,
                pr_url="https://github.com/o/r/pull/1",
                pr_state="MERGED",
            )
        ],
    )
    out = tmp_path / "pack.html"
    with (
        patch("kew.cli.preflight_tools"),
        patch("kew.cli._detect_repo"),
        patch("kew.report_export.sync_outcomes", return_value=SyncResult()),
    ):
        result = CliRunner().invoke(
            app,
            ["report", "export", "--output", str(out), "--since", "2000-01-01"],
        )
    assert result.exit_code == 0, result.output
    html = out.read_text()
    assert html.startswith("<!DOCTYPE html>")
    assert "Economics summary" in html
    # The pack inlines its logo and IBM Plex faces as base64 (#1126); strip the
    # payloads first so three random letters inside one can't read as a CDN.
    assert "cdn" not in _without_base64(html).lower()


def test_cli_report_export_json_twin(tmp_path, monkeypatch):
    import json

    from typer.testing import CliRunner

    from kew.cli import app

    monkeypatch.chdir(tmp_path)
    logs = tmp_path / "logs"
    (tmp_path / "kew.toml").write_text(
        f'[project]\nrepo = "o/r"\n[logging]\ndir = "{logs}"\n'
    )
    _seed_export_store(logs, [_sc("r1", model="opus", cost=3.0)])
    out = tmp_path / "pack.json"
    with (
        patch("kew.cli.preflight_tools"),
        patch("kew.cli._detect_repo"),
        patch("kew.report_export.sync_outcomes", return_value=SyncResult()),
    ):
        result = CliRunner().invoke(
            app,
            [
                "report",
                "export",
                "--output",
                str(out),
                "--format",
                "json",
                "--since",
                "2000-01-01",
            ],
        )
    assert result.exit_code == 0, result.output
    data = json.loads(out.read_text())
    assert data["kind"] == "kew-evidence-pack"
    assert data["economics"]["total_spend"] == 3.0


@pytest.mark.parametrize("override_cap", [1500.0, 50.0])
def test_cli_report_export_uses_db_config_overrides(
    tmp_path, monkeypatch, override_cap
):
    """A live budget override — up or down — governs the pack's caps (#1130)."""
    monkeypatch.chdir(tmp_path)
    logs = tmp_path / "logs"
    (tmp_path / "kew.toml").write_text(
        f'[project]\nrepo = "o/r"\n[logging]\ndir = "{logs}"\n'
        "[budget]\nweekly_usd = 200.0\n"
    )
    _seed_export_store(logs, [_sc("r1", model="opus", cost=3.0)])
    with SqliteRunStore(str(logs / "dispatch.db")) as store:
        store.set_config_override(
            field="budget.weekly_usd",
            value_json=json.dumps(override_cap),
            file_value_json="200.0",
            set_at=datetime.now(UTC).isoformat(),
            set_by="test",
        )

    out = tmp_path / "pack.json"
    with (
        patch("kew.cli.preflight_tools"),
        patch("kew.cli._detect_repo"),
        patch("kew.report_export.sync_outcomes", return_value=SyncResult()),
    ):
        result = CliRunner().invoke(
            app,
            ["report", "export", "--output", str(out), "--format", "json"],
        )
    assert result.exit_code == 0, result.output
    windows = json.loads(out.read_text())["economics"]["spend_windows"]
    weekly = next(w for w in windows if w["window_days"] == 7)
    assert weekly["cap"] == override_cap


def test_cli_report_export_without_dispatch_db(tmp_path, monkeypatch):
    """Export still yields an empty pack when no dispatch.db exists yet."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "kew.toml").write_text(
        f'[project]\nrepo = "o/r"\n[logging]\ndir = "{tmp_path / "logs"}"\n'
    )
    out = tmp_path / "pack.json"
    with (
        patch("kew.cli.preflight_tools"),
        patch("kew.cli._detect_repo"),
        patch("kew.report_export.sync_outcomes", return_value=SyncResult()),
    ):
        result = CliRunner().invoke(
            app,
            ["report", "export", "--output", str(out), "--format", "json"],
        )
    assert result.exit_code == 0, result.output
    assert json.loads(out.read_text())["economics"]["total_spend"] == 0.0


def test_cli_report_export_rejects_bad_format(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from kew.cli import app

    monkeypatch.chdir(tmp_path)
    (tmp_path / "kew.toml").write_text('[project]\nrepo = "o/r"\n')
    result = CliRunner().invoke(
        app, ["report", "export", "--output", str(tmp_path / "x"), "--format", "pdf"]
    )
    assert result.exit_code != 0
    assert "html" in result.output.lower()


def test_cli_report_bare_still_renders(tmp_path, monkeypatch):
    """Converting `report` to a group must not break the bare command."""
    from typer.testing import CliRunner

    from kew.cli import app

    monkeypatch.chdir(tmp_path)
    logs = tmp_path / "logs"
    (tmp_path / "kew.toml").write_text(
        f'[project]\nrepo = "o/r"\n[logging]\ndir = "{logs}"\n'
    )
    _seed_export_store(logs, [_sc("r1", model="opus", cost=1.0)])
    with (
        patch("kew.cli.preflight_tools"),
        patch("kew.cli._detect_repo"),
        patch("kew.report.sync_outcomes", return_value=SyncResult()),
    ):
        result = CliRunner().invoke(app, ["report", "--days", "3650"])
    assert result.exit_code == 0, result.output
    assert "# kew spend report" in result.output


def test_accumulate_cache_no_model_emits_no_warning():
    """_accumulate_cache_record must not emit UserWarning when model is unrecorded."""
    import warnings
    from datetime import UTC, datetime

    from kew.report import CacheGroupStats, _accumulate_cache_record
    from kew.run_store import RunRecord

    r = RunRecord(
        run_id="x",
        issue_num=1,
        branch="b",
        status="success",
        exit_code=0,
        started_at=datetime.now(UTC),
        model="",
        cache_creation_tokens=500,
        input_tokens=1000,
        output_tokens=200,
        estimated_cost_usd=0.05,
    )
    g = CacheGroupStats()
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        cold_cost, saved = _accumulate_cache_record(r, g)
    assert cold_cost == 0.0
    assert saved == 0.0


class TestReadinessProbeCohorts:
    """I05A: probe spend folds into the financial totals and nothing else."""

    def _window(self):
        from kew.run_store import READINESS_PROBE_RUN_KIND

        return [
            _record("r1", 10, cost=2.0, status="success", model="opus", item_id="10"),
            _record("r2", 11, cost=1.0, status="failure", model="opus", item_id="11"),
            _record(
                "probe-1",
                0,
                cost=0.02,
                status="success",
                model="opus",
                runner_name="probe-runner",
                item_id="probe-1",
                run_kind=READINESS_PROBE_RUN_KIND,
                title="readiness probe",
            ),
        ]

    def test_probe_spend_is_inside_total_spend_and_broken_out(self):
        stats = compute_stats(self._window(), pr_states={})
        assert stats.total_spend == pytest.approx(3.02)
        assert stats.probe_spend == pytest.approx(0.02)

    def test_probe_never_enters_the_completion_or_success_cohorts(self):
        stats = compute_stats(self._window(), pr_states={})
        assert stats.run_count == 2
        assert stats.success_count == 1
        assert stats.failure_count == 1
        assert stats.by_model["opus"].runs == 2
        assert stats.by_model["opus"].success == 1
        assert stats.by_model["opus"].total_cost == pytest.approx(3.0)
        assert "probe-runner" not in stats.by_runner
        assert stats.by_runner["claude-code"].runs == 2

    def test_probe_never_becomes_a_customer_work_item(self):
        stats = compute_stats(self._window(), pr_states={})
        assert [i.item_id for i in stats.top_issues] == ["10", "11"]

    def test_probe_spend_is_rendered_as_its_own_bucket(self):
        out = render_report(
            self._window(),
            repo="acme/widgets",
            days=7,
            now=datetime.now(UTC),
            budget=BudgetConfig(),
            pr_states={},
        )
        assert "Readiness-probe spend" in out
        assert "$0.02" in out

    def test_a_window_of_probes_alone_reports_no_work_runs(self):
        from kew.run_store import READINESS_PROBE_RUN_KIND

        stats = compute_stats(
            [
                _record(
                    "probe-1",
                    0,
                    cost=0.02,
                    run_kind=READINESS_PROBE_RUN_KIND,
                    item_id="probe-1",
                )
            ],
            pr_states={},
        )
        assert stats.total_spend == pytest.approx(0.02)
        assert stats.probe_spend == pytest.approx(0.02)
        assert stats.run_count == 0
        assert stats.median_cost is None
        assert stats.by_model == {}
        assert stats.top_issues == []

    def test_probe_presence_leaves_every_work_metric_identical(self):
        """Task 18: adding a probe to a window must not move a single number
        that describes customer work — not a run count, not a success rate,
        not a median. Only total_spend and probe_spend may change."""
        from dataclasses import asdict

        window = self._window()
        work_only = [r for r in window if r.run_kind != "readiness_probe"]

        with_probe = asdict(compute_stats(window, pr_states={}))
        without = asdict(compute_stats(work_only, pr_states={}))

        moved = {k for k in with_probe if with_probe[k] != without[k]}
        assert moved == {"total_spend", "probe_spend"}

    def test_total_spend_reconciles_to_work_plus_probe(self):
        window = self._window()
        stats = compute_stats(window, pr_states={})
        work_spend = sum(
            r.estimated_cost_usd for r in window if r.run_kind != "readiness_probe"
        )
        assert stats.total_spend == pytest.approx(work_spend + stats.probe_spend)
