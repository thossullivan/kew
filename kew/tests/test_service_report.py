"""service.report.model_scorecard DTO."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

from kew.model_eol_snapshot import LifecycleEntry
from kew.service.report import model_scorecard, spend_windows


def _rec(**kw):
    base = dict(
        model="lemonade/qwen",
        complexity="medium",
        pr_url="https://x/pr/1",
        started_at=datetime(2026, 7, 12, tzinfo=UTC),
        status="success",
        estimated_cost_usd=0.0,
        pr_state=None,
        review_cycles=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_model_scorecard_dto() -> None:
    store = MagicMock()
    store.list_since.return_value = [_rec()]
    dto = model_scorecard(store, days=3, now=datetime(2026, 7, 13, tzinfo=UTC))
    assert dto.days == 3
    assert any(r.model == "lemonade/qwen" for r in dto.rows)
    assert dto.rows[0].runs == 1
    assert dto.rows[0].lifecycle_status == ""
    store.list_since.assert_called_once_with(datetime(2026, 7, 10, tzinfo=UTC))


def test_model_scorecard_empty() -> None:
    store = MagicMock()
    store.list_since.return_value = []
    dto = model_scorecard(store, days=1, now=datetime(2026, 7, 13, tzinfo=UTC))
    assert dto.rows == []


def test_model_scorecard_includes_ordered_by_complexity() -> None:
    store = MagicMock()
    store.list_since.return_value = [
        _rec(complexity="complex", pr_url="https://x/pr/2", estimated_cost_usd=2.0),
        _rec(complexity="simple", pr_url="https://x/pr/3", estimated_cost_usd=1.0),
    ]
    dto = model_scorecard(store, days=3, now=datetime(2026, 7, 13, tzinfo=UTC))
    cells = dto.rows[0].by_complexity
    # Ordered simple -> medium -> complex regardless of input order.
    assert [c.complexity for c in cells] == ["simple", "complex"]
    assert cells[0].runs == 1
    assert cells[0].spend == 1.0
    assert cells[1].spend == 2.0


def test_spend_windows_sums_each_window() -> None:
    now = datetime(2026, 7, 15, 6, 0, tzinfo=UTC)
    store = MagicMock()
    # Windows mirror budget.check_budget: calendar-day for daily, calendar-aligned
    # 7-day (day_start - 6d) for weekly. list_since is keyed on those boundaries.
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = day_start - timedelta(days=6)
    day_runs = [
        SimpleNamespace(estimated_cost_usd=1.0),
        SimpleNamespace(estimated_cost_usd=2.0),
    ]
    week_runs = [SimpleNamespace(estimated_cost_usd=5.0)]

    def list_since(since: datetime):
        if since == day_start:
            return day_runs
        if since == week_start:
            return week_runs
        raise AssertionError(f"unexpected spend-window boundary: {since!r}")

    store.list_since.side_effect = list_since
    store.list_interactive_usage_since.return_value = []
    dto = spend_windows(store, daily_cap=10.0, weekly_cap=50.0, now=now)
    assert dto.daily.spent == 3.0
    assert dto.daily.cap == 10.0
    assert dto.daily.window_days == 1
    assert dto.weekly.spent == 5.0
    assert dto.weekly.cap == 50.0
    assert dto.weekly.window_days == 7


def test_spend_windows_passes_none_caps_through() -> None:
    now = datetime(2026, 7, 15, tzinfo=UTC)
    store = MagicMock()
    store.list_since.return_value = []
    store.list_interactive_usage_since.return_value = []
    dto = spend_windows(store, daily_cap=None, weekly_cap=None, now=now)
    assert dto.daily.cap is None
    assert dto.weekly.cap is None
    assert dto.daily.spent == 0.0


def test_spend_windows_splits_agentic_and_interactive() -> None:
    now = datetime(2026, 7, 15, tzinfo=UTC)
    store = MagicMock()
    # Agentic spend comes from run records (estimated_cost_usd).
    store.list_since.return_value = [
        SimpleNamespace(estimated_cost_usd=2.0),
        SimpleNamespace(estimated_cost_usd=1.0),
    ]
    # Interactive spend comes from the Claude Code OTel interactive_usage table.
    store.list_interactive_usage_since.return_value = [
        SimpleNamespace(cost_usd=0.5),
        SimpleNamespace(cost_usd=1.5),
    ]
    dto = spend_windows(store, daily_cap=10.0, weekly_cap=50.0, now=now)
    for cell in (dto.daily, dto.weekly):
        assert cell.agentic == 3.0
        assert cell.interactive == 2.0
        # The invariant: the window total is exactly the two segments summed.
        assert cell.spent == 5.0
        assert cell.spent == cell.agentic + cell.interactive


def test_model_scorecard_lifecycle_status() -> None:
    store = MagicMock()
    store.list_since.return_value = [_rec(model="claude-opus-5")]
    lifecycle_map = {
        "claude-opus-5": LifecycleEntry(status="ok"),
        "gpt-4": LifecycleEntry(status="retiring", shutdown="2026-12-31"),
    }
    dto = model_scorecard(
        store,
        days=3,
        now=datetime(2026, 7, 13, tzinfo=UTC),
        lifecycle_map=lifecycle_map,
    )
    row = dto.rows[0]
    assert row.lifecycle_status == "ok"

    # Model not in lifecycle_map gets empty string
    store.list_since.return_value = [_rec(model="unknown-model")]
    dto2 = model_scorecard(
        store,
        days=3,
        now=datetime(2026, 7, 13, tzinfo=UTC),
        lifecycle_map=lifecycle_map,
    )
    assert dto2.rows[0].lifecycle_status == ""
