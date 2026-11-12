import json
from datetime import UTC, datetime
from pathlib import Path

from typer.testing import CliRunner

from kew.cli import app
from kew.run_store import SqliteRunStore

runner = CliRunner()


def _write_config_and_fixture(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    (tmp_path / "kew.toml").write_text(
        f'[project]\nrepo = "o/r"\n[logging]\ndir = "{logs}"\n'
    )
    fixture = json.loads(
        (Path(__file__).parent / "fixtures" / "claude_code_otel.json").read_text()
    )
    export = tmp_path / "export.json"
    export.write_text(json.dumps(fixture))
    return logs, export


def test_ingest_file_writes_rows(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    logs, export = _write_config_and_fixture(tmp_path)

    result = runner.invoke(app, ["ingest", "file", str(export)])
    assert result.exit_code == 0, result.output
    assert "ingested 3" in result.output.lower()

    with SqliteRunStore(str(logs / "dispatch.db")) as store:
        rows = store.list_interactive_usage_since(datetime(2024, 1, 1, tzinfo=UTC))
        assert len(rows) == 3


def test_ingest_file_missing_path_errors(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_config_and_fixture(tmp_path)
    result = runner.invoke(app, ["ingest", "file", str(tmp_path / "nope.json")])
    assert result.exit_code == 2


def test_ingest_serve_requires_enabled(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_config_and_fixture(tmp_path)  # config has no [ingest] -> disabled
    result = runner.invoke(app, ["ingest", "serve"])
    assert result.exit_code == 2
    assert "enabled" in result.output.lower()
