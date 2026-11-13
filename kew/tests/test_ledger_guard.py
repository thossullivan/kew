"""Forked-ledger detection under one project root (#1156)."""

from pathlib import Path

from kew.ledger_guard import find_stray_ledgers, stray_ledger_warning


def _ledger(base: Path) -> Path:
    db = base / "logs" / "dispatch" / "dispatch.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    db.write_bytes(b"")
    return db


def test_no_warning_when_only_the_canonical_ledger_exists(tmp_path: Path):
    canonical = _ledger(tmp_path)
    (tmp_path / "kew").mkdir()

    assert find_stray_ledgers(tmp_path, canonical) == []
    assert stray_ledger_warning(tmp_path, canonical) is None


def test_finds_a_ledger_minted_from_a_subdirectory(tmp_path: Path):
    canonical = _ledger(tmp_path)
    stray = _ledger(tmp_path / "kew")

    assert find_stray_ledgers(tmp_path, canonical) == [stray]


def test_warning_names_every_fork_and_the_canonical_ledger(tmp_path: Path):
    canonical = _ledger(tmp_path)
    stray = _ledger(tmp_path / "www")

    warning = stray_ledger_warning(tmp_path, canonical)

    assert warning is not None
    assert str(stray) in warning
    assert str(canonical) in warning
    assert "COUNT-SHRINK" in warning


def test_root_ledger_is_a_fork_when_the_canonical_one_lives_elsewhere(
    tmp_path: Path,
):
    """An absolute logging.dir makes the conventional repo-root DB the stray."""
    canonical = tmp_path / "elsewhere" / "dispatch.db"
    canonical.parent.mkdir(parents=True)
    canonical.write_bytes(b"")
    stray = _ledger(tmp_path / "project")

    assert find_stray_ledgers(tmp_path / "project", canonical) == [stray]


def test_a_nested_project_with_its_own_config_is_not_a_fork(tmp_path: Path):
    """Monorepos with per-service control planes are deliberate, not forks."""
    canonical = _ledger(tmp_path)
    service = tmp_path / "svc-a"
    _ledger(service)
    (service / "kew.toml").write_text('[project]\nname = "svc-a"\n', encoding="utf-8")

    assert find_stray_ledgers(tmp_path, canonical) == []


def test_a_nested_dispatch_toml_does_not_make_a_project(tmp_path: Path):
    """W4.3 row 19: only kew.toml marks a nested project; dispatch.toml does not."""
    canonical = _ledger(tmp_path)
    service = tmp_path / "svc-a"
    stray = _ledger(service)
    (service / "dispatch.toml").write_text("[project]\n", encoding="utf-8")

    assert find_stray_ledgers(tmp_path, canonical) == [stray]


def test_root_config_does_not_excuse_a_leftover_root_ledger(tmp_path: Path):
    """The root always has a kew.toml — that must not mask its own stale DB."""
    (tmp_path / "kew.toml").write_text("[project]\n", encoding="utf-8")
    canonical = tmp_path / "elsewhere" / "dispatch.db"
    canonical.parent.mkdir(parents=True)
    canonical.write_bytes(b"")
    stray = _ledger(tmp_path)

    assert find_stray_ledgers(tmp_path, canonical) == [stray]


def test_scan_skips_dotdirs_and_pruned_trees(tmp_path: Path):
    canonical = _ledger(tmp_path)
    _ledger(tmp_path / ".git" / "worktrees")
    _ledger(tmp_path / "node_modules" / "pkg")

    assert find_stray_ledgers(tmp_path, canonical) == []


def test_scan_stops_at_max_depth(tmp_path: Path):
    canonical = _ledger(tmp_path)
    deep = _ledger(tmp_path / "a" / "b" / "c")

    assert find_stray_ledgers(tmp_path, canonical, max_depth=1) == []
    assert find_stray_ledgers(tmp_path, canonical, max_depth=3) == [deep]


def test_cli_warns_about_a_forked_ledger(tmp_path: Path, monkeypatch):
    """Preflight names the fork instead of silently ignoring it (#1156)."""
    from unittest.mock import patch

    from kew.cli import _emit_stray_ledger_warnings
    from kew.config import load_config

    project = (tmp_path / "repo").resolve()
    (project / ".git").mkdir(parents=True)
    (project / "kew.toml").write_text(
        '[logging]\ndir = "./logs/dispatch"\n', encoding="utf-8"
    )
    _ledger(project)
    stray = _ledger(project / "pkg")
    monkeypatch.chdir(project)
    config = load_config()

    with patch("kew.cli._warn") as mock_warn:
        _emit_stray_ledger_warnings(config)

    assert str(stray) in mock_warn.call_args.args[0]


def test_cli_stays_silent_and_never_raises_without_a_fork(tmp_path: Path, monkeypatch):
    from unittest.mock import patch

    from kew.cli import _emit_stray_ledger_warnings
    from kew.config import DispatchConfig

    monkeypatch.chdir(tmp_path)
    with patch("kew.cli._warn") as mock_warn:
        _emit_stray_ledger_warnings(DispatchConfig())
    mock_warn.assert_not_called()

    with (
        patch("kew.ledger_guard.stray_ledger_warning", side_effect=RuntimeError("x")),
        patch("kew.cli._warn") as mock_warn,
    ):
        _emit_stray_ledger_warnings(DispatchConfig())  # must not raise
    mock_warn.assert_not_called()


def test_unreadable_directory_never_raises(tmp_path: Path, monkeypatch):
    canonical = _ledger(tmp_path)
    (tmp_path / "kew").mkdir()

    def _boom(self: Path):
        raise PermissionError(str(self))

    monkeypatch.setattr(Path, "iterdir", _boom)
    assert find_stray_ledgers(tmp_path, canonical) == []
