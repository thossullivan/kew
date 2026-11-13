"""CLI boundary catches SchemaTooNewError and prints an actionable message."""

import sqlite3

from typer.testing import CliRunner

from kew.cli import app
from kew.links import UPGRADE_HINT

runner = CliRunner()


def _poison_db(tmp_path):
    """Create a dispatch.db whose schema version is too new for this kew."""
    from kew.run_store import CURRENT_SCHEMA_VERSION, SqliteRunStore

    db = tmp_path / "logs" / "dispatch.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    with SqliteRunStore(str(db)):
        pass
    conn = sqlite3.connect(str(db))
    conn.execute(
        "UPDATE schema_version SET version = ? WHERE id = 1",
        (CURRENT_SCHEMA_VERSION + 1,),
    )
    conn.commit()
    conn.close()
    return CURRENT_SCHEMA_VERSION + 1, CURRENT_SCHEMA_VERSION


def _cfg_with_log_dir(tmp_path):
    from kew.config import DispatchConfig

    cfg = DispatchConfig()
    cfg.logging.dir = str(tmp_path / "logs")
    return cfg


class TestSchemaTooNewBoundary:
    """Opening a too-new dispatch.db via any CLI command yields a clean message."""

    def _setup(self, monkeypatch, tmp_path):
        cfg = _cfg_with_log_dir(tmp_path)
        monkeypatch.setattr("kew.cli.load_config", lambda: cfg)
        monkeypatch.setattr("kew.cli.project_root", lambda: tmp_path)
        monkeypatch.setattr("kew.cli.resolved_config_path", lambda: None)
        monkeypatch.setattr("kew.cli._preflight", lambda *a, **k: None)
        monkeypatch.setattr("kew.cli._refuse_if_fleet_stopped", lambda *a, **k: None)
        return _poison_db(tmp_path)

    def _assert_clean_error(self, result, db_ver, max_ver):
        assert result.exit_code != 0, result.output
        assert "Traceback" not in result.output
        assert f"v{db_ver}" in result.output
        assert f"v{max_ver}" in result.output
        assert UPGRADE_HINT in result.output
        assert "git+" not in result.output
        assert "dispatch.db" in result.output

    def test_status_shows_clean_error(self, monkeypatch, tmp_path):
        db_ver, max_ver = self._setup(monkeypatch, tmp_path)
        result = runner.invoke(app, ["status"])
        self._assert_clean_error(result, db_ver, max_ver)

    def test_health_shows_clean_error(self, monkeypatch, tmp_path):
        db_ver, max_ver = self._setup(monkeypatch, tmp_path)
        result = runner.invoke(app, ["health"])
        self._assert_clean_error(result, db_ver, max_ver)

    def test_refuse_if_fleet_stopped_shows_clean_error(self, monkeypatch, tmp_path):
        """_refuse_if_fleet_stopped opens SqliteRunStore directly."""
        from kew.config import DispatchConfig

        cfg = DispatchConfig()
        cfg.logging.dir = str(tmp_path / "logs")
        _poison_db(tmp_path)
        monkeypatch.setattr("kew.cli.load_config", lambda: cfg)
        monkeypatch.setattr("kew.cli.project_root", lambda: tmp_path)

        import typer as _typer

        from kew.cli import _refuse_if_fleet_stopped

        try:
            _refuse_if_fleet_stopped(cfg)
            raised = False
        except _typer.Exit:
            raised = True
        except Exception:
            raised = False

        assert raised, (
            "_refuse_if_fleet_stopped must catch SchemaTooNewError "
            "and raise typer.Exit, not propagate the raw exception"
        )

    def test_ingest_serve_shows_clean_error(self, monkeypatch, tmp_path):
        db_ver, max_ver = self._setup(monkeypatch, tmp_path)
        monkeypatch.setattr(
            "kew.cli.load_config",
            lambda: _cfg_with_ingest(tmp_path),
        )
        result = runner.invoke(app, ["ingest", "serve"])
        self._assert_clean_error(result, db_ver, max_ver)

    def test_serve_shows_clean_error(self, monkeypatch, tmp_path):
        db_ver, max_ver = self._setup(monkeypatch, tmp_path)
        monkeypatch.setattr("kew.cli.preflight_tools", lambda: None)
        # serve probes the DB at startup before touching git/uvicorn
        result = runner.invoke(app, ["serve"])
        self._assert_clean_error(result, db_ver, max_ver)


def _cfg_with_ingest(tmp_path):
    from kew.config import DispatchConfig

    cfg = DispatchConfig()
    cfg.logging.dir = str(tmp_path / "logs")
    cfg.ingest.enabled = True
    return cfg
