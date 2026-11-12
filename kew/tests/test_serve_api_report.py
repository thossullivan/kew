"""GET /api/report/models endpoint test."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from kew.budget import check_budget
from kew.config import BudgetConfig
from kew.serve.app import create_app
from kew.service.report import spend_windows


def _rec():
    return SimpleNamespace(
        model="lemonade/qwen",
        complexity="medium",
        pr_url="https://x/pr/1",
        started_at=datetime(2026, 7, 12, tzinfo=UTC),
        status="success",
        estimated_cost_usd=0.0,
        pr_state=None,
        review_cycles=None,
    )


def _client(tmp_path: Path, inbox_client) -> TestClient:
    store = MagicMock()
    store.list_since.return_value = [_rec()]

    @contextmanager
    def factory():
        yield store

    app = create_app(store_factory=factory, repo_dir=tmp_path)
    return inbox_client(app, loopback_peer=True)


def test_report_models_endpoint(tmp_path: Path, inbox_client) -> None:
    r = _client(tmp_path, inbox_client).get("/api/report/models?days=3")
    assert r.status_code == 200
    body = r.json()
    assert body["days"] == 3
    assert body["rows"][0]["model"] == "lemonade/qwen"


def test_report_models_default_days(tmp_path: Path, inbox_client) -> None:
    r = _client(tmp_path, inbox_client).get("/api/report/models")
    assert r.status_code == 200
    assert r.json()["days"] == 3


def test_report_spend_endpoint(
    tmp_path: Path, inbox_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The spend endpoint loads effective config via load_config(store=...),
    # which searches for kew.toml from cwd upward — run from tmp_path so the
    # test asserts BudgetConfig DEFAULTS, not whatever caps the developer's
    # repo kew.toml happens to set (broke when #632 raised daily to 100).
    (tmp_path / ".git").mkdir()  # Bound upward configuration discovery.
    monkeypatch.chdir(tmp_path)
    store = MagicMock()
    store.list_since.return_value = [SimpleNamespace(estimated_cost_usd=4.0)]
    store.list_config_overrides.return_value = []  # no DB overrides -> toml defaults

    @contextmanager
    def factory():
        yield store

    app = create_app(store_factory=factory, repo_dir=tmp_path)
    r = inbox_client(app, loopback_peer=True).get("/api/report/spend")
    assert r.status_code == 200
    body = r.json()
    assert body["daily"]["window_days"] == 1
    assert body["weekly"]["window_days"] == 7
    assert body["daily"]["spent"] == 4.0
    # Caps come from effective config (BudgetConfig defaults: 50 daily / 200 weekly).
    assert body["daily"]["cap"] == 50.0
    assert body["weekly"]["cap"] == 200.0


def test_enforced_daily_figure_matches_budget_enforcement() -> None:
    """The web's enforced (agentic) daily figure must equal budget.py's
    enforcement computation on the same fixture: calendar-day window,
    agentic-only, interactive spend excluded (N5).

    Against the pre-N5 rolling-24h + agentic+interactive bar this fails: the
    yesterday-evening run and the interactive usage both leak into the figure
    the bar compares to the calendar-day agentic-only cap.
    """
    now = datetime(2026, 7, 15, 6, 0, tzinfo=UTC)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    records = [
        # Inside today's calendar day — enforced.
        SimpleNamespace(
            estimated_cost_usd=3.0,
            started_at=day_start + timedelta(hours=1),
            status="success",
        ),
        # Yesterday evening: within a rolling 24h window but NOT today's
        # calendar day — the governor excludes it, so the web bar must too.
        SimpleNamespace(
            estimated_cost_usd=5.0,
            started_at=day_start - timedelta(hours=2),
            status="success",
        ),
    ]

    class FakeStore:
        def list_since(self, since: datetime) -> list[SimpleNamespace]:
            return [r for r in records if r.started_at >= since]

        def list_interactive_usage_since(
            self, since: datetime
        ) -> list[SimpleNamespace]:
            # Observed interactive spend — must never enter the enforced figure.
            return [SimpleNamespace(cost_usd=99.0)]

        def list_by_issue(self, issue: int) -> list[SimpleNamespace]:
            return []

        def list_by_item(self, item_id: str) -> list[SimpleNamespace]:
            return []

    store = FakeStore()
    budget = BudgetConfig()
    status = check_budget(store, budget, issue_num=1, now=now)  # type: ignore[arg-type]
    dto = spend_windows(
        store,
        daily_cap=budget.daily_usd,
        weekly_cap=budget.weekly_usd,
        now=now,
    )

    assert status.daily_spent_usd == 3.0
    # The enforced (agentic) figure is byte-identical to the governor's.
    assert dto.daily.agentic == status.daily_spent_usd
    # Interactive is observed-only; it inflates the total but never the enforced
    # figure the bar gates against the cap.
    assert dto.daily.interactive == 99.0
    assert dto.daily.spent == dto.daily.agentic + dto.daily.interactive
    assert dto.daily.agentic != dto.daily.spent
