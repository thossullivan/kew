"""Test isolation: no CLI test may open the repo-local dispatch.db (SFD-134 / kew #784).

Background: ~20 CLI/integration tests invoke the CLI with a bare
``DispatchConfig()`` whose default ``logging.dir`` is the CWD-relative
``./logs/dispatch``. Run from ``kew/``, that is the *repo-local*
``logs/dispatch/dispatch.db`` — so the suite's result depended on filesystem
state left by *previous* runs of *other* code. A newer branch's schema
migration would auto-upgrade that DB and then every older branch died on
``SchemaTooNewError`` for a full false-negative verify cycle.

The fix redirects the *default* log dir via ``config._DEFAULT_LOG_DIR_OVERRIDE``
(set by the autouse ``_hermetic_log_dir`` fixture in conftest), so every test —
including the ones that build the config themselves — is pointed at an isolated
tmp dir. It is a default, not an override: a kew.toml value or an explicit
``logging.dir`` still wins.
"""

from pathlib import Path

import kew.config as kew_config
from kew.config import DispatchConfig, LoggingConfig

_REPO_ROOT = Path(__file__).resolve().parent.parent
_REPO_DISPATCH_DB = _REPO_ROOT / "logs" / "dispatch" / "dispatch.db"


def test_logging_dir_default_honors_kew_log_dir(monkeypatch, tmp_path):
    """With the test override cleared, a bare DispatchConfig() reads KEW_LOG_DIR."""
    target = tmp_path / "somewhere" / "logs"
    monkeypatch.setattr(kew_config, "_DEFAULT_LOG_DIR_OVERRIDE", None)
    monkeypatch.setenv("KEW_LOG_DIR", str(target))
    assert DispatchConfig().logging.dir == str(target)
    assert LoggingConfig().dir == str(target)


def test_logging_dir_default_falls_back_when_unset(monkeypatch):
    """Without override or env, the historical ./logs/dispatch default is kept."""
    monkeypatch.setattr(kew_config, "_DEFAULT_LOG_DIR_OVERRIDE", None)
    monkeypatch.delenv("KEW_LOG_DIR", raising=False)
    assert LoggingConfig().dir == "./logs/dispatch"


def test_explicit_dir_wins_over_override(monkeypatch, tmp_path):
    """The override is a default, not an override: an explicit dir still wins."""
    monkeypatch.setattr(kew_config, "_DEFAULT_LOG_DIR_OVERRIDE", str(tmp_path / "def"))
    cfg = DispatchConfig()
    cfg.logging.dir = "./custom-logs"
    assert cfg.logging.dir == "./custom-logs"
    # A dir supplied at construction (as kew.toml does) is not replaced either.
    assert LoggingConfig(dir="./from-toml").dir == "./from-toml"


def test_autouse_fixture_redirects_bare_config_off_repo():
    """Under the autouse _hermetic_log_dir fixture, a bare config never resolves
    to the repo-local logs/dispatch dir."""
    resolved = Path(DispatchConfig().logging.dir).resolve()
    assert not str(resolved).startswith(str(_REPO_ROOT)), (
        f"bare DispatchConfig().logging.dir resolved inside the repo: {resolved}"
    )
    assert resolved != _REPO_DISPATCH_DB.parent


def test_override_points_outside_repo_for_every_test():
    """The autouse fixture sets the default-dir override outside the repo."""
    override = kew_config._DEFAULT_LOG_DIR_OVERRIDE
    assert override is not None
    assert not Path(override).resolve().is_relative_to(_REPO_ROOT)
