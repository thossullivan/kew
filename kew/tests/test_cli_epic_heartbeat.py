"""``kew epic heartbeat --waiting``: a loop says it is waiting on purpose (#2566)."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

from typer.testing import CliRunner

from kew.cli import app
from kew.config import DispatchConfig, ProjectConfig
from kew.run_store import SqliteRunStore
from kew.service.epics import read_driver_waiting

runner = CliRunner()


def _config(tmp_path: Path) -> DispatchConfig:
    cfg = DispatchConfig(project=ProjectConfig(repo="o/r"))
    cfg.logging.dir = str(tmp_path / "logs")
    return cfg


@patch("kew.cli.load_config")
def test_heartbeat_records_the_wait_and_its_reason(
    load_cfg: Any, tmp_path: Path
) -> None:
    load_cfg.return_value = _config(tmp_path)

    result = runner.invoke(
        app, ["epic", "heartbeat", "host-contract", "--waiting", "slot"]
    )

    assert result.exit_code == 0, result.output
    assert "host-contract: waiting (slot)" in result.output
    with SqliteRunStore(str(tmp_path / "logs" / "dispatch.db")) as store:
        beat = read_driver_waiting(store, "host-contract")
    assert beat is not None
    assert beat.reason == "slot"


@patch("kew.cli.load_config")
def test_heartbeat_refuses_an_empty_reason(load_cfg: Any, tmp_path: Path) -> None:
    load_cfg.return_value = _config(tmp_path)

    result = runner.invoke(
        app, ["epic", "heartbeat", "host-contract", "--waiting", " "]
    )

    assert result.exit_code == 2
    assert not (tmp_path / "logs" / "dispatch.db").exists()
