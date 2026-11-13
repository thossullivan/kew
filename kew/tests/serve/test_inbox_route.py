"""After retiring inbox.html, GET /inbox serves the SPA shell, not the old page."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from kew.serve.app import create_app


@contextmanager
def _fake_store_factory():
    yield MagicMock()


def _client(tmp_path: Path) -> TestClient:
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("<!doctype html><div id='app'>SPA SHELL</div>")
    app = create_app(
        store_factory=lambda: _fake_store_factory(),
        repo_dir=tmp_path,
        static_dir=web,
    )
    # Host-header middleware requires the loopback base_url.
    return TestClient(app, base_url="http://127.0.0.1:8765")


def test_inbox_serves_the_spa_shell(tmp_path: Path) -> None:
    r = _client(tmp_path).get("/inbox")
    assert r.status_code == 200
    assert "SPA SHELL" in r.text


def test_unknown_api_path_still_404s_as_json(tmp_path: Path) -> None:
    r = _client(tmp_path).get("/api/nope")
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("application/json")
