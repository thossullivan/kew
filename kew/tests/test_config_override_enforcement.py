"""Tests that config overrides are enforced on the dispatch/budget path (#404).

The mechanism (load_config(store=…) merges DB→toml→env) was already tested in
test_config.py. This file verifies the *wiring*: that the enforcement callers
actually pass a store so overrides take effect.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest

from kew.budget import BudgetExceeded, check_budget
from kew.cli import DispatchCommandResult
from kew.config import load_config
from kew.run_store import RunRecord, SqliteRunStore


def _seed_spend(store: SqliteRunStore, issue_num: int, cost: float) -> None:
    store.save(
        RunRecord(
            run_id=f"r-{issue_num}",
            issue_num=issue_num,
            branch=f"agent/issue-{issue_num}",
            status="success",
            exit_code=0,
            runner_name="claude-code",
            model="claude-sonnet-4-6",
            complexity="medium",
            started_at=datetime.now(UTC),
            duration_seconds=60.0,
            estimated_cost_usd=cost,
        )
    )


def _set_override(store, field, value, file_value=0.0):
    store.set_config_override(
        field=field,
        value_json=json.dumps(value),
        file_value_json=json.dumps(file_value),
        set_at=datetime.now(UTC).isoformat(),
        set_by="test",
        reason="",
        audit_event_id=None,
    )


# ── check_budget honours DB-overridden caps ──────────────────────────


class TestCheckBudgetWithOverrides:
    """check_budget uses the BudgetConfig it receives — so if config was
    loaded with store=, the overridden caps apply."""

    @pytest.fixture
    def store(self, tmp_path):
        with SqliteRunStore(str(tmp_path / "test.db")) as s:
            yield s

    def test_override_lowers_daily_cap_trips_budget(self, store, tmp_path):
        toml = tmp_path / "kew.toml"
        toml.write_text("[budget]\ndaily_usd = 100.0\n")

        _set_override(store, "budget.daily_usd", 5.0, 100.0)

        cfg = load_config(config_path=toml, store=store)
        assert cfg.budget.daily_usd == 5.0

        _seed_spend(store, issue_num=1, cost=6.0)
        with pytest.raises(BudgetExceeded) as exc_info:
            check_budget(store, cfg.budget, issue_num=99)
        assert exc_info.value.which_limit == "daily"

    def test_file_cap_would_pass_but_override_trips(self, store, tmp_path):
        toml = tmp_path / "kew.toml"
        toml.write_text("[budget]\nper_issue_usd = 100.0\n")

        _seed_spend(store, issue_num=42, cost=8.0)

        file_cfg = load_config(config_path=toml)
        check_budget(store, file_cfg.budget, issue_num=42)

        _set_override(store, "budget.per_issue_usd", 5.0, 100.0)
        cfg = load_config(config_path=toml, store=store)
        with pytest.raises(BudgetExceeded):
            check_budget(store, cfg.budget, issue_num=42)

    def test_env_beats_db_override(self, store, tmp_path, monkeypatch):
        toml = tmp_path / "kew.toml"
        toml.write_text("[agent]\nmax_budget_usd = 10.0\n")

        _set_override(store, "agent.max_budget_usd", 20.0, 10.0)

        monkeypatch.setenv("KEW_MAX_BUDGET", "50.0")
        cfg = load_config(config_path=toml, store=store)
        assert cfg.agent.max_budget_usd == 50.0


# ── CLI enforcement path wires store into load_config ────────────────


class TestDispatchPathUsesOverrides:
    """The run_cmd / dispatch commands reload config with store= so that
    DB overrides reach check_budget and the rest of the enforcement path."""

    def _write_toml(self, tmp_path):
        log_dir = tmp_path / "logs" / "dispatch"
        log_dir.mkdir(parents=True, exist_ok=True)
        toml = tmp_path / "kew.toml"
        toml.write_text(
            f"[project]\nrepo = 'test/repo'\n"
            f'[logging]\ndir = "{log_dir}"\n'
            f"[budget]\ndaily_usd = 100.0\n"
            f"[agent]\nmax_budget_usd = 25.0\n"
        )
        return toml, log_dir

    def test_run_cmd_reloads_config_with_store(self, tmp_path, monkeypatch):
        toml, log_dir = self._write_toml(tmp_path)

        db_path = log_dir / "dispatch.db"
        with SqliteRunStore(str(db_path)) as store:
            _set_override(store, "agent.max_budget_usd", 33.0, 25.0)

        captured_config = {}

        def fake_dispatch_one(config, **kwargs):
            captured_config["budget"] = config.agent.max_budget_usd
            return DispatchCommandResult("executed")

        monkeypatch.setattr(
            "kew.cli.load_config",
            lambda store=None: load_config(config_path=toml, store=store),
        )
        monkeypatch.setattr("kew.cli._dispatch_one", fake_dispatch_one)
        monkeypatch.setattr("kew.cli._preflight", lambda *a, **kw: None)
        monkeypatch.setattr("kew.cli._refuse_if_fleet_stopped", lambda *a: None)
        monkeypatch.setattr("kew.cli.build_notifier", lambda *a: None)

        from kew.cli import run_cmd

        run_cmd(
            drain=False,
            dry_run=False,
            show_prompt=False,
            workers=None,
            force=False,
            model=None,
        )

        assert captured_config["budget"] == 33.0

    def test_dispatch_cmd_reloads_config_with_store(self, tmp_path, monkeypatch):
        toml, log_dir = self._write_toml(tmp_path)

        db_path = log_dir / "dispatch.db"
        with SqliteRunStore(str(db_path)) as store:
            _set_override(store, "budget.per_issue_usd", 99.0, 15.0)

        captured_config = {}

        def fake_dispatch_one(config, **kwargs):
            captured_config["per_issue"] = config.budget.per_issue_usd
            return DispatchCommandResult("executed")

        monkeypatch.setattr(
            "kew.cli.load_config",
            lambda store=None: load_config(config_path=toml, store=store),
        )
        monkeypatch.setattr("kew.cli._dispatch_one", fake_dispatch_one)
        monkeypatch.setattr("kew.cli._preflight", lambda *a, **kw: None)
        monkeypatch.setattr("kew.cli._refuse_if_fleet_stopped", lambda *a: None)
        monkeypatch.setattr("kew.cli.build_notifier", lambda *a: None)

        from kew.cli import dispatch

        dispatch(issue=1, loop=False, max_steps=None)

        assert captured_config["per_issue"] == 99.0


# ── API GET /api/config/budget reflects enforcement truth ────────────


class TestApiGetBudgetMatchesEnforcement:
    """GET /api/config/budget effective must equal what load_config(store=…)
    produces — including env precedence."""

    def _make_app(self, tmp_path, db):
        from kew.serve.app import create_app
        from kew.serve.auth import SessionStore

        @contextmanager
        def factory():
            s = SqliteRunStore(db)
            try:
                yield s
            finally:
                s.close()

        return create_app(
            lambda: factory(),
            repo_dir=tmp_path,
            config=load_config(),
            token="T",
            sessions=SessionStore(),
            bind_host="127.0.0.1",
            bind_port=8765,
        )

    def test_effective_matches_load_config_with_store(self, tmp_path, inbox_client):
        db = str(tmp_path / "kew.db")
        with SqliteRunStore(db) as store:
            _set_override(store, "budget.daily_usd", 42.0, 50.0)

        app = self._make_app(tmp_path, db)
        client = inbox_client(app, loopback_peer=True)
        r = client.get("/api/config/budget")
        assert r.status_code == 200
        info = r.json()["budget.daily_usd"]
        assert info["effective"] == 42.0

    def test_api_effective_equals_enforcement(self, tmp_path, inbox_client):
        """Whatever load_config(store=…) resolves is what the API reports."""
        db = str(tmp_path / "kew.db")
        with SqliteRunStore(db) as store:
            _set_override(store, "budget.per_issue_usd", 77.0, 15.0)

        app = self._make_app(tmp_path, db)
        client = inbox_client(app, loopback_peer=True)

        r = client.get("/api/config/budget")
        api_effective = r.json()["budget.per_issue_usd"]["effective"]

        with SqliteRunStore(db) as store:
            enforced = load_config(store=store)
        assert api_effective == enforced.budget.per_issue_usd == 77.0
