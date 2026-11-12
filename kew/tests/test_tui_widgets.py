from datetime import UTC, datetime, timedelta


class TestQueuePanel:
    def test_compact_render(self):
        from kew.tui.widgets.queue_panel import format_queue_row_compact

        issue = {
            "number": 142,
            "title": "Fix auth regression in login flow",
            "labels": [{"name": "ready"}, {"name": "p0-critical"}, {"name": "complex"}],
            "createdAt": "2026-03-20T10:00:00Z",
        }
        line = format_queue_row_compact(
            issue, routed_runner="claude-code", routed_model="claude-opus-4"
        )
        assert "#142" in line
        assert "P0" in line or "p0" in line.lower()
        assert "complex" in line.lower()

    def test_compact_truncates_long_title(self):
        from kew.tui.widgets.queue_panel import format_queue_row_compact

        issue = {
            "number": 1,
            "title": "A" * 100,
            "labels": [{"name": "ready"}],
            "createdAt": "2026-03-20T10:00:00Z",
        }
        line = format_queue_row_compact(
            issue, routed_runner="claude-code", routed_model="sonnet"
        )
        from rich.text import Text

        visible_len = len(Text.from_markup(line).plain)
        assert visible_len < 100


class TestWorkersPanel:
    def test_compact_render(self):
        from kew.run_store import RunRecord
        from kew.tui.widgets.workers_panel import format_worker_row_compact

        record = RunRecord(
            run_id="r1",
            issue_num=135,
            branch="b1",
            status="pending",
            exit_code=-1,
            started_at=datetime.now(UTC),
            runner_name="claude-code",
            model="claude-sonnet-4",
        )
        snapshot = {
            "total_tokens": 42000,
            "cost_usd": 1.80,
            "last_activity": "Editing config.py",
        }
        line = format_worker_row_compact(
            record,
            snapshot=snapshot,
            expected_tokens=100000,
            issue_title="Refactor config parser",
        )
        assert "#135" in line
        assert "42.0k" in line

    def test_compact_render_linear_item_bare(self):
        from kew.run_store import RunRecord
        from kew.tui.widgets.workers_panel import format_worker_row_compact

        record = RunRecord(
            run_id="r1",
            issue_num=0,
            item_id="SFD-12",
            branch="b1",
            status="pending",
            exit_code=-1,
            started_at=datetime.now(UTC),
            runner_name="claude-code",
            model="claude-sonnet-4",
        )
        from rich.text import Text

        line = format_worker_row_compact(
            record,
            snapshot={"total_tokens": 1000},
            expected_tokens=100000,
            issue_title="Linear item",
        )
        plain = Text.from_markup(line).plain
        assert "SFD-12" in plain
        assert "#SFD-12" not in plain  # bare, no GitHub hash
        assert "#0" not in plain  # never surface the sentinel

    def test_completed_row_linear_item_bare(self):
        from kew.run_store import RunRecord
        from kew.tui.widgets.workers_panel import format_completed_row

        now = datetime.now(UTC)
        record = RunRecord(
            run_id="c1",
            issue_num=0,
            item_id="SFD-12",
            branch="b1",
            status="success",
            exit_code=0,
            started_at=now - timedelta(minutes=30),
            finished_at=now - timedelta(minutes=5),
            duration_seconds=1500,
            estimated_cost_usd=2.35,
            title="",  # falls back to the item ref
        )
        from rich.text import Text

        plain = Text.from_markup(format_completed_row(record, now=now)).plain
        assert "SFD-12" in plain
        assert "#SFD-12" not in plain
        assert "#0" not in plain

    def test_progress_bar_capped_at_99(self):
        from kew.tui.widgets.workers_panel import progress_bar

        bar = progress_bar(current=200000, expected=100000, width=10)
        assert "99%" in bar

    def test_completed_row_format(self):
        from kew.run_store import RunRecord
        from kew.tui.widgets.workers_panel import format_completed_row

        now = datetime.now(UTC)
        record = RunRecord(
            run_id="c1",
            issue_num=42,
            branch="b1",
            status="success",
            exit_code=0,
            started_at=now - timedelta(minutes=30),
            finished_at=now - timedelta(minutes=5),
            duration_seconds=1500,
            estimated_cost_usd=2.35,
            title="Fix auth bug",
        )
        text = format_completed_row(record, now=now)
        assert "#42" in text
        assert "Fix auth bug" in text
        assert "25m" in text  # duration
        assert "$2.35" in text  # cost
        assert "5m ago" in text  # time since completion

    def test_completed_row_error_icon(self):
        from kew.run_store import RunRecord
        from kew.tui.widgets.workers_panel import format_completed_row

        now = datetime.now(UTC)
        record = RunRecord(
            run_id="c2",
            issue_num=99,
            branch="b2",
            status="error",
            exit_code=1,
            started_at=now - timedelta(hours=2),
            finished_at=now - timedelta(hours=1),
            duration_seconds=3600,
            estimated_cost_usd=5.00,
            title="Broken thing",
        )
        text = format_completed_row(record, now=now)
        assert "ER" in text
        assert "1h ago" in text

    def test_completed_row_fixed_width_columns(self):
        """Duration and cost columns should have consistent width."""
        from rich.text import Text

        from kew.run_store import RunRecord
        from kew.tui.widgets.workers_panel import format_completed_row

        now = datetime.now(UTC)
        short_run = RunRecord(
            run_id="s1",
            issue_num=1,
            branch="b",
            status="success",
            exit_code=0,
            started_at=now - timedelta(minutes=5),
            finished_at=now - timedelta(minutes=2),
            duration_seconds=180,
            estimated_cost_usd=0.12,
            title="Short task",
        )
        long_run = RunRecord(
            run_id="s2",
            issue_num=2,
            branch="b",
            status="success",
            exit_code=0,
            started_at=now - timedelta(hours=3),
            finished_at=now - timedelta(minutes=30),
            duration_seconds=9000,
            estimated_cost_usd=12.50,
            title="Long task",
        )
        short_plain = Text.from_markup(format_completed_row(short_run, now=now)).plain
        long_plain = Text.from_markup(format_completed_row(long_run, now=now)).plain
        # Both rows should have the same total length (fixed-width columns)
        assert len(short_plain) == len(long_plain)

    def test_completed_row_large_days_stays_fixed_width(self):
        """Days >= 100 should still produce a 7-char time-ago field."""
        from rich.text import Text

        from kew.run_store import RunRecord
        from kew.tui.widgets.workers_panel import format_completed_row

        now = datetime.now(UTC)
        record = RunRecord(
            run_id="c4",
            issue_num=7,
            branch="b",
            status="success",
            exit_code=0,
            started_at=now - timedelta(days=200),
            finished_at=now - timedelta(days=150),
            duration_seconds=600,
            estimated_cost_usd=1.00,
            title="Old task",
        )
        text = format_completed_row(record, now=now)
        assert ">99d" in text
        # Verify fixed-width still holds against a short-duration row
        short_record = RunRecord(
            run_id="c5",
            issue_num=7,
            branch="b",
            status="success",
            exit_code=0,
            started_at=now - timedelta(minutes=15),
            finished_at=now - timedelta(minutes=5),
            duration_seconds=600,
            estimated_cost_usd=1.00,
            title="Old task",
        )
        long_plain = Text.from_markup(text).plain
        short_plain = Text.from_markup(
            format_completed_row(short_record, now=now)
        ).plain
        assert len(long_plain) == len(short_plain)

    def test_completed_row_no_finished_at_fallback(self):
        """When finished_at is None, time-since shows n/a."""
        from kew.run_store import RunRecord
        from kew.tui.widgets.workers_panel import format_completed_row

        now = datetime.now(UTC)
        record = RunRecord(
            run_id="c3",
            issue_num=10,
            branch="b",
            status="success",
            exit_code=0,
            started_at=now - timedelta(minutes=10),
            finished_at=None,
            duration_seconds=600,
            estimated_cost_usd=1.00,
            title="No finish time",
        )
        text = format_completed_row(record, now=now)
        assert "n/a" in text.lower() or "—" in text


class TestStatusBar:
    def test_format(self):
        from kew.tui.widgets.status_bar import format_status_bar

        text = format_status_bar(
            project="my-project",
            daily_spend=4.20,
            daily_limit=50.0,
            worker_count=2,
            queue_ready_count=14,
            queue_backlog_count=3,
        )
        assert "my-project" in text
        assert "$4.20" in text
        assert "2" in text
        assert "14" in text
        assert "3" in text

    def test_spend_shows_tilde_prefix(self):
        """Spend shows ~ to indicate it is estimated, not actual billing."""
        from kew.tui.widgets.status_bar import format_status_bar

        text = format_status_bar(
            project="proj",
            daily_spend=3.50,
            daily_limit=50.0,
            worker_count=1,
            queue_ready_count=0,
            queue_backlog_count=0,
        )
        assert "~$3.50" in text

    def test_spend_shows_daily_label(self):
        """Spend limit shows 'daily' label to indicate cumulative daily scope."""
        from kew.tui.widgets.status_bar import format_status_bar

        text = format_status_bar(
            project="proj",
            daily_spend=3.50,
            daily_limit=50.0,
            worker_count=1,
            queue_ready_count=0,
            queue_backlog_count=0,
        )
        assert "daily" in text

    def test_active_cost_included_in_spend(self):
        """Active worker cost is included in the displayed spend total."""
        from kew.tui.widgets.status_bar import format_status_bar

        text = format_status_bar(
            project="proj",
            daily_spend=3.00,
            daily_limit=50.0,
            worker_count=1,
            queue_ready_count=0,
            queue_backlog_count=0,
            active_cost=1.50,
        )
        # Total should be 3.00 + 1.50 = 4.50
        assert "~$4.50" in text


class TestBudgetOverlay:
    def test_format_budget_summary(self):
        from kew.tui.widgets.budget_overlay import format_budget_summary

        text = format_budget_summary(
            daily_spend=4.20,
            daily_limit=50.0,
            weekly_spend=18.50,
            weekly_limit=200.0,
            per_issue_avg=3.08,
            per_issue_limit=15.0,
            burn_rate_per_hour=1.40,
            hours_until_daily_limit=32.7,
        )
        assert "$4.20" in text
        assert "$50" in text or "50.00" in text
        assert "burn" in text.lower() or "$1.40" in text


class TestHelpOverlay:
    def test_format_help(self):
        from kew.tui.widgets.help_overlay import format_help_text

        text = format_help_text()
        assert "1/2/3" in text or "switch" in text.lower()
        assert "Tab" in text  # cockpit shows Tab, not Esc
        assert "q" in text

    def test_format_help_screen_aware(self):
        from kew.tui.widgets.help_overlay import format_help_text

        expanded = format_help_text("expanded")
        assert "Esc" in expanded
        assert "J/K" in expanded

        logs = format_help_text("logs")
        assert "Esc" in logs
        assert "h" in logs
        # Logs screen shows action keys including kill-worker
        assert "Dispatch" in logs
        assert "Kill" in logs
        # Expanded screen does NOT show kill-worker
        assert "Kill" not in expanded


def test_confirm_dialog_compose():
    from kew.tui.widgets.confirm_dialog import ConfirmDialog

    dialog = ConfirmDialog("Delete item?")
    assert dialog.prompt == "Delete item?"
    widgets = list(dialog.compose())
    assert len(widgets) == 2


def test_filter_overlay_compose():
    from kew.tui.screens.filter_overlay import FilterOverlay

    overlay = FilterOverlay(panel_id="queue-panel")
    widgets = list(overlay.compose())
    type_names = [type(w).__name__ for w in widgets]
    assert "Input" in type_names


class TestFormatTokens:
    def test_zero(self):
        from kew.tui.formatting import format_tokens

        assert format_tokens(0) == "0"

    def test_small(self):
        from kew.tui.formatting import format_tokens

        assert format_tokens(500) == "500"

    def test_thousands(self):
        from kew.tui.formatting import format_tokens

        assert format_tokens(45000) == "45.0k"

    def test_millions(self):
        from kew.tui.formatting import format_tokens

        assert format_tokens(1_200_000) == "1.2M"


def test_worker_row_shows_token_count():
    """Worker row displays token count when available."""
    from kew.run_store import RunRecord
    from kew.tui.widgets.workers_panel import format_worker_row_compact

    record = RunRecord(
        run_id="w1",
        issue_num=42,
        branch="b1",
        status="pending",
        exit_code=-1,
        started_at=datetime.now(UTC),
        model="claude-sonnet-4",
    )
    snapshot = {"total_tokens": 45000, "cost_usd": 0.0, "last_activity": "reading file"}
    text = format_worker_row_compact(
        record, snapshot=snapshot, expected_tokens=100000, issue_title="Fix bug"
    )
    assert "45.0k" in text


def test_worker_row_shows_zero_tokens_gracefully():
    """Worker row handles zero tokens."""
    from kew.run_store import RunRecord
    from kew.tui.widgets.workers_panel import format_worker_row_compact

    record = RunRecord(
        run_id="w1",
        issue_num=42,
        branch="b1",
        status="pending",
        exit_code=-1,
        started_at=datetime.now(UTC),
        model="claude-sonnet-4",
    )
    snapshot = {"total_tokens": 0, "cost_usd": 0.0, "last_activity": ""}
    text = format_worker_row_compact(
        record, snapshot=snapshot, expected_tokens=100000, issue_title="Fix bug"
    )
    assert "0%" in text


def test_status_bar_shows_active_tokens():
    """Status bar includes token burn from active workers."""
    from kew.tui.widgets.status_bar import format_status_bar

    text = format_status_bar(
        project="kew",
        daily_spend=3.50,
        daily_limit=50.0,
        worker_count=2,
        queue_ready_count=5,
        queue_backlog_count=0,
        active_tokens=128000,
    )
    assert "128.0k" in text


class TestReviewsPanel:
    def test_compact_render(self):
        from kew.tui.widgets.reviews_panel import format_review_row_compact

        pr = {
            "number": 87,
            "title": "Fix auth token refresh flow",
            "author": {"login": "thossullivan"},
            "createdAt": "2026-03-22T10:00:00Z",
            "comments": [{"id": 1}, {"id": 2}, {"id": 3}],
        }
        line = format_review_row_compact(pr)
        assert "PR #87" in line or "#87" in line
        from rich.text import Text

        plain = Text.from_markup(line).plain
        assert "3 comments" in plain

    def test_compact_truncates_long_title(self):
        from kew.tui.widgets.reviews_panel import format_review_row_compact

        pr = {
            "number": 1,
            "title": "A" * 100,
            "author": {"login": "alice"},
            "createdAt": "2026-03-22T10:00:00Z",
            "comments": [],
        }
        line = format_review_row_compact(pr)
        from rich.text import Text

        visible_len = len(Text.from_markup(line).plain)
        assert visible_len < 100


class TestFormatBudgetStatusLines:
    def test_all_within_budget(self):
        from kew.budget import BudgetStatus
        from kew.tui.widgets.budget_overlay import format_budget_status_lines

        status = BudgetStatus(
            issue_spent_usd=5.0,
            issue_limit_usd=15.0,
            daily_spent_usd=20.0,
            daily_limit_usd=50.0,
            weekly_spent_usd=80.0,
            weekly_limit_usd=200.0,
        )
        text = format_budget_status_lines(status)
        assert "$20.00" in text
        assert "$50.00" in text
        assert "$5.00" in text
        # All within budget — no red x markers
        from rich.text import Text

        plain = Text.from_markup(text).plain
        assert "✗" not in plain

    def test_daily_exceeded_shows_marker(self):
        from kew.budget import BudgetStatus
        from kew.tui.widgets.budget_overlay import format_budget_status_lines

        status = BudgetStatus(
            issue_spent_usd=5.0,
            issue_limit_usd=15.0,
            daily_spent_usd=51.0,
            daily_limit_usd=50.0,
            weekly_spent_usd=80.0,
            weekly_limit_usd=200.0,
        )
        text = format_budget_status_lines(status)
        from rich.text import Text

        plain = Text.from_markup(text).plain
        assert "102%" in plain

    def test_multiple_exceeded(self):
        from kew.budget import BudgetStatus
        from kew.tui.widgets.budget_overlay import format_budget_status_lines

        status = BudgetStatus(
            issue_spent_usd=20.0,
            issue_limit_usd=15.0,
            daily_spent_usd=55.0,
            daily_limit_usd=50.0,
            weekly_spent_usd=80.0,
            weekly_limit_usd=200.0,
        )
        text = format_budget_status_lines(status)
        from rich.text import Text

        plain = Text.from_markup(text).plain
        # Both issue and daily should show exceeded percentages
        assert "133%" in plain  # 20/15
        assert "110%" in plain  # 55/50

    def test_zero_limit_handled_gracefully(self):
        from kew.budget import BudgetStatus
        from kew.tui.widgets.budget_overlay import format_budget_status_lines

        status = BudgetStatus(
            issue_spent_usd=0.0,
            issue_limit_usd=0.0,
            daily_spent_usd=0.0,
            daily_limit_usd=0.0,
            weekly_spent_usd=0.0,
            weekly_limit_usd=0.0,
        )
        # Should not raise ZeroDivisionError
        text = format_budget_status_lines(status)
        assert isinstance(text, str)


class TestBudgetExceededDialog:
    def test_compose_yields_widgets(self):
        from kew.budget import BudgetStatus
        from kew.tui.widgets.budget_exceeded_dialog import BudgetExceededDialog

        status = BudgetStatus(
            issue_spent_usd=5.0,
            issue_limit_usd=15.0,
            daily_spent_usd=51.0,
            daily_limit_usd=50.0,
            weekly_spent_usd=80.0,
            weekly_limit_usd=200.0,
        )
        dialog = BudgetExceededDialog(
            title="Budget Exceeded",
            issue_label="#42: Fix auth token refresh",
            status=status,
        )
        widgets = list(dialog.compose())
        assert len(widgets) >= 3  # title, issue label, budget lines, hint

    def test_stores_attributes(self):
        from kew.budget import BudgetStatus
        from kew.tui.widgets.budget_exceeded_dialog import BudgetExceededDialog

        status = BudgetStatus(
            issue_spent_usd=5.0,
            issue_limit_usd=15.0,
            daily_spent_usd=51.0,
            daily_limit_usd=50.0,
            weekly_spent_usd=80.0,
            weekly_limit_usd=200.0,
        )
        dialog = BudgetExceededDialog(
            title="Force Dispatch",
            issue_label="#42: Fix auth",
            status=status,
            confirm_key="d",
        )
        assert dialog.budget_status is status

    def test_default_confirm_key_is_d(self):
        from kew.budget import BudgetStatus
        from kew.tui.widgets.budget_exceeded_dialog import BudgetExceededDialog

        status = BudgetStatus(
            issue_spent_usd=0.0,
            issue_limit_usd=15.0,
            daily_spent_usd=0.0,
            daily_limit_usd=50.0,
            weekly_spent_usd=0.0,
            weekly_limit_usd=200.0,
        )
        dialog = BudgetExceededDialog(
            title="Test",
            issue_label="#1: Test",
            status=status,
        )
        assert dialog._confirm_key == "d"

    def test_custom_confirm_key(self):
        from kew.budget import BudgetStatus
        from kew.tui.widgets.budget_exceeded_dialog import BudgetExceededDialog

        status = BudgetStatus(
            issue_spent_usd=0.0,
            issue_limit_usd=15.0,
            daily_spent_usd=0.0,
            daily_limit_usd=50.0,
            weekly_spent_usd=0.0,
            weekly_limit_usd=200.0,
        )
        dialog = BudgetExceededDialog(
            title="Test",
            issue_label="#1: Test",
            status=status,
            confirm_key="r",
        )
        assert dialog._confirm_key == "r"


class TestDetailPaneCache:
    def test_cache_returns_same_object_for_same_issue(self):
        from kew.tui.widgets.detail_pane import _RENDERED_CACHE, _get_cached_renderable

        _RENDERED_CACHE.clear()
        issue = {
            "number": 42,
            "title": "Test issue",
            "body": "Some **markdown** body",
            "labels": [],
            "assignees": [],
            "createdAt": "2026-03-20",
        }
        result1 = _get_cached_renderable(issue)
        result2 = _get_cached_renderable(issue)
        assert result1 is result2  # Same object from cache

    def test_cache_differentiates_by_body(self):
        from kew.tui.widgets.detail_pane import _RENDERED_CACHE, _get_cached_renderable

        _RENDERED_CACHE.clear()
        issue1 = {
            "number": 42,
            "title": "Test",
            "body": "Body A",
            "labels": [],
            "assignees": [],
            "createdAt": "",
        }
        issue2 = {
            "number": 42,
            "title": "Test",
            "body": "Body B",
            "labels": [],
            "assignees": [],
            "createdAt": "",
        }
        result1 = _get_cached_renderable(issue1)
        result2 = _get_cached_renderable(issue2)
        assert result1 is not result2  # Different body = different cache entry

    def test_cache_evicts_beyond_max(self):
        from kew.tui.widgets.detail_pane import (
            _RENDERED_CACHE,
            _RENDERED_CACHE_MAX,
            _get_cached_renderable,
        )

        _RENDERED_CACHE.clear()
        for i in range(_RENDERED_CACHE_MAX + 5):
            issue = {
                "number": i,
                "title": f"Issue {i}",
                "body": f"Body {i}",
                "labels": [],
                "assignees": [],
                "createdAt": "",
            }
            _get_cached_renderable(issue)
        assert len(_RENDERED_CACHE) == _RENDERED_CACHE_MAX

    def test_cache_lru_recency(self):
        """Refreshing an entry keeps it alive; the true LRU entry is evicted."""
        from kew.tui.widgets.detail_pane import (
            _RENDERED_CACHE,
            _RENDERED_CACHE_MAX,
            _cache_key,
            _get_cached_renderable,
        )

        _RENDERED_CACHE.clear()

        def _make_issue(i):
            return {
                "number": i,
                "title": f"Issue {i}",
                "body": f"Body {i}",
                "labels": [],
                "assignees": [],
                "createdAt": "",
            }

        # Fill cache to capacity
        for i in range(_RENDERED_CACHE_MAX):
            _get_cached_renderable(_make_issue(i))

        # Touch the first entry (issue 0) to mark it as recently used
        _get_cached_renderable(_make_issue(0))

        # Add one more entry to trigger eviction
        _get_cached_renderable(_make_issue(_RENDERED_CACHE_MAX))

        # Issue 0 should survive (was recently used)
        assert _cache_key(_make_issue(0)) in _RENDERED_CACHE
        # Issue 1 should be evicted (was the true LRU entry)
        assert _cache_key(_make_issue(1)) not in _RENDERED_CACHE
        assert len(_RENDERED_CACHE) == _RENDERED_CACHE_MAX


class TestStatusBarFleetStopped:
    def test_banner_prepended_when_latched(self):
        from kew.tui.widgets.status_bar import format_status_bar

        bar = format_status_bar(
            project="kew",
            daily_spend=1.0,
            daily_limit=50.0,
            worker_count=0,
            queue_ready_count=0,
            queue_backlog_count=0,
            fleet_stopped={
                "stopped_at": "2026-06-09T10:00:00+00:00",
                "reason": "runaway",
            },
        )
        assert "FLEET STOPPED" in bar
        assert "runaway" in bar

    def test_no_banner_by_default(self):
        from kew.tui.widgets.status_bar import format_status_bar

        bar = format_status_bar(
            project="kew",
            daily_spend=1.0,
            daily_limit=50.0,
            worker_count=0,
            queue_ready_count=0,
            queue_backlog_count=0,
        )
        assert "FLEET STOPPED" not in bar
