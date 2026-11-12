"""Tests for effective config observability."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from kew.cli import app
from kew.cli.config import _snapshot, _valid_overrides
from kew.config import load_config
from kew.run_store import SqliteRunStore
from kew.service.config import human_review_state, set_human_review_override

runner = CliRunner()


@pytest.mark.parametrize("error", [OSError("unreadable"), ValueError("invalid")])
def test_effective_resolution_failure_exits_two(error: Exception) -> None:
    with patch("kew.cli.config._open_effective_snapshot", side_effect=error):
        result = runner.invoke(app, ["config", "effective", "--json"])

    assert result.exit_code == 2, result.output
    assert "Could not resolve effective config" in result.output
    assert "Traceback" not in result.output


def test_config_effective_sources_names_the_winning_layer(tmp_path: Path) -> None:
    values = {
        "budget.per_issue_usd": {"value": 75.0, "source": "DB override"},
        "agent.max_budget_usd": {
            "value": 100.0,
            "source": "env (KEW_MAX_BUDGET)",
        },
    }
    with patch(
        "kew.cli.config._open_effective_snapshot",
        return_value=(values, tmp_path / "kew.toml", tmp_path / "dispatch.db"),
    ):
        result = runner.invoke(app, ["config", "effective", "--sources"])

    assert result.exit_code == 0, result.output
    assert "budget.per_issue_usd = 75.0  # source: DB override" in result.output
    assert "agent.max_budget_usd = 100.0  # source: env (KEW_MAX_BUDGET)" in (
        result.output
    )


def test_config_effective_json_can_omit_sources(tmp_path: Path) -> None:
    values = {
        "budget.per_issue_usd": {"value": 75.0, "source": "DB override"},
    }
    with patch(
        "kew.cli.config._open_effective_snapshot",
        return_value=(values, tmp_path / "kew.toml", tmp_path / "dispatch.db"),
    ):
        result = runner.invoke(app, ["config", "effective", "--json"])

    assert result.exit_code == 0, result.output
    assert result.output.strip() == '{\n  "budget.per_issue_usd": 75.0\n}'


def test_config_snapshot_matches_dispatch_precedence(
    tmp_path: Path, monkeypatch
) -> None:
    toml = tmp_path / "kew.toml"
    toml.write_text("[budget]\nper_issue_usd = 10.0\n", encoding="utf-8")
    monkeypatch.setenv("KEW_MAX_BUDGET", "100")

    with SqliteRunStore(str(tmp_path / "dispatch.db")) as store:
        store.set_config_override(
            field="budget.per_issue_usd",
            value_json="75.0",
            file_value_json="10.0",
            set_at="2026-08-05T00:00:00+00:00",
            set_by="test",
        )
        effective = load_config(config_path=toml, store=store)
        snapshot = _snapshot(
            effective,
            config_path=toml,
            overrides=_valid_overrides(store),
        )

    assert snapshot["budget.per_issue_usd"] == {
        "value": 75.0,
        "source": "DB override",
    }
    assert snapshot["agent.max_budget_usd"] == {
        "value": 100.0,
        "source": "env (KEW_MAX_BUDGET)",
    }


def test_config_snapshot_never_attributes_a_removed_env_name(
    tmp_path: Path, monkeypatch
) -> None:
    """W4.3 row 18: DISPATCH_* names are neither applied nor shown as a source."""
    toml = tmp_path / "kew.toml"
    toml.write_text("[agent]\nmax_budget_usd = 10.0\n", encoding="utf-8")
    monkeypatch.delenv("KEW_MAX_BUDGET", raising=False)
    monkeypatch.setenv("DISPATCH_MAX_BUDGET", "100")

    effective = load_config(config_path=toml)
    snapshot = _snapshot(effective, config_path=toml, overrides={})

    assert snapshot["agent.max_budget_usd"] == {"value": 10.0, "source": "kew.toml"}


# ── the human-review policy in `kew config effective` (I13C) ────────


def _review_config(repo: str = "acme/widgets"):
    cfg = load_config().model_copy(deep=True)
    cfg.project.repo = repo
    return cfg


def test_human_review_fields_read_the_durable_policy_not_the_file(
    tmp_path: Path,
) -> None:
    """The CLI must render I13's override, never the kew.toml value it hides."""
    cfg = _review_config()
    with SqliteRunStore(str(tmp_path / "dispatch.db")) as store:
        set_human_review_override(
            store, field="human_review.max_diff_lines", value=200, config=cfg
        )
        state = human_review_state(store, config=cfg)
        snapshot = _snapshot(cfg, config_path=None, overrides={}, review=state)

    assert snapshot["human_review.max_diff_lines"]["value"] == 200
    assert snapshot["human_review.max_diff_lines"]["source"] == (
        "human_review policy (generation 1)"
    )
    # Untouched policy fields still come from the same in-force policy.
    assert snapshot["human_review.max_paths"]["value"] == 20


def test_human_review_snapshot_matches_the_service_state(tmp_path: Path) -> None:
    cfg = _review_config()
    with SqliteRunStore(str(tmp_path / "dispatch.db")) as store:
        set_human_review_override(
            store,
            field="human_review.max_pending_reviews",
            value=1,
            config=cfg,
            expires_at="2099-01-01T00:00:00+00:00",
        )
        state = human_review_state(store, config=cfg)
        snapshot = _snapshot(cfg, config_path=None, overrides={}, review=state)

    for field, value in state.values.items():
        assert snapshot[field]["value"] == value
    entry = snapshot["human_review.max_pending_reviews"]
    assert entry["generation"] == 1
    assert entry["expires_at"] == "2099-01-01T00:00:00+00:00"
    assert entry["over_cap"] is False
    assert "expires 2099-01-01T00:00:00+00:00" in entry["source"]


def test_human_review_falls_back_to_the_config_layer_without_a_policy(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(_review_config(), config_path=None, overrides={}, review=None)
    assert snapshot["human_review.max_diff_lines"] == {
        "value": 500,
        "source": "default",
    }


def test_config_effective_renders_human_review_sources(tmp_path: Path) -> None:
    values = {
        "human_review.max_diff_lines": {
            "value": 200,
            "source": "human_review policy (generation 1)",
            "generation": 1,
            "expires_at": None,
            "over_cap": False,
        },
    }
    with patch(
        "kew.cli.config._open_effective_snapshot",
        return_value=(values, tmp_path / "kew.toml", tmp_path / "dispatch.db"),
    ):
        result = runner.invoke(app, ["config", "effective", "--sources"])

    assert result.exit_code == 0, result.output
    assert (
        "human_review.max_diff_lines = 200"
        "  # source: human_review policy (generation 1)"
    ) in result.output


def test_config_effective_json_keeps_the_human_review_detail(tmp_path: Path) -> None:
    values = {
        "human_review.max_paths": {
            "value": 5,
            "source": "human_review policy (generation 2)",
            "generation": 2,
            "expires_at": None,
            "over_cap": True,
        },
    }
    with patch(
        "kew.cli.config._open_effective_snapshot",
        return_value=(values, tmp_path / "kew.toml", tmp_path / "dispatch.db"),
    ):
        result = runner.invoke(app, ["config", "effective", "--json", "--sources"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["human_review.max_paths"] == {
        "value": 5,
        "source": "human_review policy (generation 2)",
        "generation": 2,
        "expires_at": None,
        "over_cap": True,
    }


@pytest.mark.parametrize("enabled", ["auto", True, False])
def test_effective_config_records_uncontained_flows_noop(tmp_path, enabled):
    from kew.config import DispatchConfig, SandboxConfig

    config = DispatchConfig(
        sandbox=SandboxConfig(enabled=enabled, allow_uncontained_flows=True)
    )
    toml = tmp_path / "kew.toml"
    toml.write_text("[sandbox]\nallow_uncontained_flows = true\n")
    values = _snapshot(config, config_path=toml, overrides={})
    entry = values["sandbox.allow_uncontained_flows"]
    assert entry["value"] is (enabled is True)
    if enabled is True:
        assert entry["source"] == "kew.toml"
    else:
        assert entry["configured_value"] is True
        assert entry["source"] == "kew.toml (no-op: requires sandbox.enabled = true)"
