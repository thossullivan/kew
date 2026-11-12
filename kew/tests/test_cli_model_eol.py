"""CLI behavior for `kew model-eol check` over the curated lifecycle registry.

The feed, its URL, and `kew model-eol sync` are gone (#1490): `check` reads the
committed registry that `load_config` already hydrated and never writes or
fetches anything. Exit 1 is reserved for a configured or explicit model id with
no curated row; every lifecycle status stays advisory at exit 0.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from kew.cli import app
from kew.cli._apps import model_eol_app
from kew.config import DispatchConfig, RouteEntry, RoutingConfig, load_config
from kew.model_eol_snapshot import LifecycleEntry

runner = CliRunner()
MODEL = "claude-opus-5"
MISSING = "vendor/never-curated"


def _config(**entries: LifecycleEntry) -> DispatchConfig:
    """A config whose only checked id is ``MODEL``, with hydrated lifecycle rows."""
    config = DispatchConfig()
    config.agent.model = MODEL
    config.model_eol.snapshot_models = dict(entries)
    return config


def _invoke(config: DispatchConfig, *args: str) -> Any:
    with patch("kew.cli.model_eol.load_config", return_value=config):
        return runner.invoke(app, ["model-eol", "check", *args])


def _registry(*rows: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": "kew/model-lifecycle-registry@1",
        "reviewedAt": "2026-08-16",
        "models": list(rows),
    }


def _row(model_id: str, *, status: str = "ok") -> dict[str, Any]:
    return {
        "id": model_id,
        "provider": "vendor",
        "status": status,
        "shutdown": None,
        "replacement": None,
        "evidence": [
            {
                "kind": "provider-lifecycle-doc",
                "url": "https://docs.example.invalid/deprecations",
                "checkedAt": "2026-08-16",
            }
        ],
    }


# --------------------------------------------------------------------------
# The removed surface
# --------------------------------------------------------------------------


def test_model_eol_group_help_describes_registry_check_not_feed_sync() -> None:
    result = runner.invoke(app, ["model-eol", "--help"])

    assert result.exit_code == 0, result.output
    assert "sync" not in result.output.lower()
    assert "feed" not in result.output.lower()
    assert "registry" in result.output.lower()
    assert "check" in result.output.lower()


def test_model_eol_group_help_is_declared_statically_not_mutated_at_runtime() -> None:
    """`_apps.py` owns the help text; nothing rewrites it while commands load."""
    declared = model_eol_app.info.help or ""

    assert "registry" in declared.lower()
    assert "read-only" in declared.lower()
    assert "sync" not in declared.lower()


def test_sync_command_is_gone() -> None:
    result = runner.invoke(app, ["model-eol", "sync"])

    assert result.exit_code != 0
    assert "sync" not in (model_eol_app.info.help or "").lower()


# --------------------------------------------------------------------------
# Exit 1: a configured or explicit id with no curated row
# --------------------------------------------------------------------------


def test_check_explicit_missing_model_exits_one() -> None:
    result = _invoke(
        _config(**{MODEL: LifecycleEntry(status="ok")}), "--model", MISSING
    )

    assert result.exit_code == 1, result.output
    assert MISSING in result.output
    assert "registry gap" in result.output


def test_check_configured_model_without_a_row_exits_one() -> None:
    result = _invoke(_config())

    assert result.exit_code == 1, result.output
    assert MODEL in result.output


def test_check_covers_configured_agent_reviewer_routing_and_pricing_ids() -> None:
    config = DispatchConfig()
    config.agent.model = "vendor/agent"
    config.review.model = "vendor/reviewer"
    config.routing = RoutingConfig(
        default=RouteEntry(runner="claude", model="vendor/default"),
        complex=RouteEntry(runner="claude", model="vendor/complex"),
    )
    config.pricing.overrides = {"vendor/override": [1.0, 2.0]}

    result = _invoke(config)

    assert result.exit_code == 1, result.output
    for model in (
        "vendor/agent",
        "vendor/reviewer",
        "vendor/default",
        "vendor/complex",
        "vendor/override",
    ):
        assert model in result.output


# --------------------------------------------------------------------------
# Exit 0: every lifecycle status is advisory
# --------------------------------------------------------------------------


def test_untracked_and_actionable_rows_are_advisory_exit_zero() -> None:
    config = _config(
        **{
            MODEL: LifecycleEntry(status="untracked", provider="anthropic"),
            "vendor/retiring": LifecycleEntry(
                status="retiring", shutdown="2026-12-01", replacement="vendor/next"
            ),
        }
    )

    result = _invoke(config, "--model", "vendor/retiring")

    assert result.exit_code == 0, result.output
    assert "lifecycle unverified" in result.output
    assert "status=retiring" in result.output
    assert "shutdown=2026-12-01" in result.output


def test_check_of_an_ok_row_is_quiet_and_exits_zero() -> None:
    result = _invoke(_config(**{MODEL: LifecycleEntry(status="ok")}))

    assert result.exit_code == 0, result.output
    assert "lifecycle unverified" not in result.output
    assert MODEL not in result.output


def test_check_without_configured_or_explicit_ids_exits_zero() -> None:
    result = _invoke(DispatchConfig())

    assert result.exit_code == 0, result.output


# --------------------------------------------------------------------------
# A malformed registry is an error, not exit-1 drift
# --------------------------------------------------------------------------


def test_invalid_registry_input_is_a_distinct_error_not_a_coverage_gap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A real broken registry file, through the real load path: the failure is
    # the config envelope (exit 2, docs/cli/model-eol.md), never the coverage
    # gap code 1, and the registry's own reason reaches the operator.
    payload = _registry(_row(MODEL))
    payload["reviewedAt"] = "whenever"
    (tmp_path / "lifecycle.json").write_text(json.dumps(payload), encoding="utf-8")
    (tmp_path / "kew.toml").write_text(
        f'[agent]\nmodel = "{MODEL}"\n[model_eol]\nsnapshot_path = "lifecycle.json"\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["model-eol", "check"])

    assert result.exit_code == 2, result.output
    assert result.exit_code != 1
    assert "Traceback" not in result.output
    assert "reviewedAt" in result.output


# --------------------------------------------------------------------------
# The command is read-only and offline, and honors the explicit override
# --------------------------------------------------------------------------


def test_check_honors_the_explicit_snapshot_path_override(tmp_path: Path) -> None:
    registry = tmp_path / "lifecycle.json"
    registry.write_text(json.dumps(_registry(_row(MODEL))), encoding="utf-8")
    config_path = tmp_path / "kew.toml"
    config_path.write_text(
        f'[agent]\nmodel = "{MODEL}"\n[model_eol]\nsnapshot_path = "lifecycle.json"\n',
        encoding="utf-8",
    )

    result = _invoke(load_config(config_path))

    assert result.exit_code == 0, result.output


def test_check_reads_an_override_registry_and_reports_its_gaps(tmp_path: Path) -> None:
    registry = tmp_path / "lifecycle.json"
    registry.write_text(json.dumps(_registry(_row("vendor/other"))), encoding="utf-8")
    config_path = tmp_path / "kew.toml"
    config_path.write_text(
        f'[agent]\nmodel = "{MODEL}"\n[model_eol]\nsnapshot_path = "lifecycle.json"\n',
        encoding="utf-8",
    )

    result = _invoke(load_config(config_path))

    assert result.exit_code == 1, result.output
    assert MODEL in result.output


def test_check_never_writes_and_never_opens_a_socket(tmp_path: Path) -> None:
    config = _config(**{MODEL: LifecycleEntry(status="retired")})
    before = sorted(path.name for path in tmp_path.iterdir())

    with (
        patch("socket.socket", side_effect=AssertionError("network")),
        patch("pathlib.Path.write_text", side_effect=AssertionError("write")),
        patch("pathlib.Path.write_bytes", side_effect=AssertionError("write")),
    ):
        result = _invoke(config)

    assert result.exit_code == 0, result.output
    assert sorted(path.name for path in tmp_path.iterdir()) == before


@pytest.mark.parametrize("flag", ["--check", "--feed-url"])
def test_removed_sync_flags_are_not_accepted(flag: str) -> None:
    result = _invoke(_config(**{MODEL: LifecycleEntry(status="ok")}), flag)

    assert result.exit_code != 0
