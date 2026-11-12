"""`kew plan show` reads the recorded plan out of the ledger (#2290)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from kew.cli import app
from kew.config import DispatchConfig, LoggingConfig
from kew.ledger_identity import open_managed_store
from kew.triage.record import record_extracted_plan

runner = CliRunner()

_BODY = """### Acceptance

- The ledger has a row per body digest.
- `kew plan show 2278` prints the criteria.
"""


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    """A project ledger at <tmp>/logs/dispatch.db, with load_config pointed at it."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    monkeypatch.chdir(tmp_path)
    config = DispatchConfig(logging=LoggingConfig(dir=str(log_dir)))
    with patch("kew.cli.load_config", return_value=config):
        yield log_dir / "dispatch.db"


def _open(db_path: Path):
    """Open through the managed boundary, so the CLI's read-only open is allowed."""
    root = db_path.parent.parent
    return open_managed_store(
        project_root=root,
        db_path=db_path,
        anchors_path=root / "audit-anchors.jsonl",
    )


def _record(db_path: Path, body: str, item: str = "2278") -> None:
    with _open(db_path) as store:
        record_extracted_plan(store, item_id=item, body=body, run_id="run-1")


def test_show_prints_the_sha_source_and_criteria(ledger):
    _record(ledger, _BODY)

    result = runner.invoke(app, ["plan", "show", "2278"])

    assert result.exit_code == 0, result.output
    assert "current: 2278" in result.output
    assert "source=extracted" in result.output
    assert "criteria=2" in result.output
    assert "AC1: The ledger has a row per body digest." in result.output
    assert "AC2: `kew plan show 2278` prints the criteria." in result.output
    assert "(not accepted by a person)" in result.output


def test_an_item_with_no_recorded_plan_says_so_and_exits_two(ledger):
    _record(ledger, _BODY)

    result = runner.invoke(app, ["plan", "show", "9999"])

    assert result.exit_code == 2
    assert "no plan recorded for 9999" in result.output


def test_a_missing_ledger_names_the_path_it_looked_for(ledger):
    result = runner.invoke(app, ["plan", "show", "2278"])

    assert result.exit_code == 2
    assert "no ledger at" in result.output
    assert "dispatch.db" in result.output


def test_a_body_stating_no_criteria_prints_a_zero_criteria_plan(ledger):
    _record(ledger, "Just prose.")

    result = runner.invoke(app, ["plan", "show", "2278"])

    assert result.exit_code == 0, result.output
    assert "criteria=0" in result.output


def test_history_lists_the_superseded_plans_before_the_current_one(ledger):
    _record(ledger, _BODY)
    _record(ledger, _BODY + "- A third criterion.\n")

    result = runner.invoke(app, ["plan", "show", "2278", "--history"])

    assert result.exit_code == 0, result.output
    assert result.output.index("superseded: 2278") < result.output.index(
        "current: 2278"
    )
    assert "criteria=2" in result.output
    assert "criteria=3" in result.output


def test_history_is_off_by_default(ledger):
    _record(ledger, _BODY)
    _record(ledger, _BODY + "- A third criterion.\n")

    result = runner.invoke(app, ["plan", "show", "2278"])

    assert result.exit_code == 0, result.output
    assert "superseded" not in result.output


def test_an_unreadable_stored_plan_refuses_rather_than_printing_no_criteria(ledger):
    _record(ledger, _BODY)
    with _open(ledger) as store:
        store._conn.execute("UPDATE item_plans SET plan_json = 'not json'")
        store._conn.commit()

    result = runner.invoke(app, ["plan", "show", "2278"])

    assert result.exit_code == 2
    assert "unreadable" in result.output


def test_a_ledger_predating_recorded_plans_refuses_with_exit_two(ledger):
    """#2309 P1: the read-only open cannot migrate, so a pre-v60 ledger reaches
    the query as "no such table" — that must be the documented exit 2, not an
    unhandled sqlite3 traceback."""
    _record(ledger, _BODY)
    with _open(ledger) as store:
        store._conn.execute("DROP TABLE item_plans")
        store._conn.execute("UPDATE schema_version SET version = 59")
        store._conn.commit()

    result = runner.invoke(app, ["plan", "show", "2278"])

    assert result.exit_code == 2, result.output
    assert "cannot read plans from" in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)
