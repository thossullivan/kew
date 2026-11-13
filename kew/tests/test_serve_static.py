"""The web bundle and the API coexist under `kew serve`."""

from contextlib import nullcontext
from pathlib import Path

from kew.serve.app import create_app


class _EmptyStore:
    """Minimal RunStore double — only list_all() is exercised by these tests."""

    def list_all(self):
        return []


def test_serves_index_and_api_coexist(tmp_path: Path, inbox_client):
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("<!doctype html><title>kew</title>")
    app = create_app(
        lambda: nullcontext(_EmptyStore()), repo_dir=tmp_path, static_dir=web
    )
    client = inbox_client(app, loopback_peer=True)

    # Static root serves the SPA shell...
    root = client.get("/")
    assert root.status_code == 200
    assert "<title>kew</title>" in root.text

    # ...and the API still works (routes match before the static mount).
    api = client.get("/api/runs")
    assert api.status_code == 200
    assert api.json() == []


def test_no_static_dir_serves_hint_not_bare_404(tmp_path: Path, inbox_client):
    # With no built bundle the API still works, and `/` renders a loud hint
    # page (see test_serve_bundle.py) instead of the old bare 404.
    app = create_app(lambda: nullcontext(_EmptyStore()), repo_dir=tmp_path)
    client = inbox_client(app, loopback_peer=True)
    assert client.get("/api/runs").status_code == 200
    root = client.get("/")
    assert root.status_code == 200
    assert "not built" in root.text


def test_create_app_wires_security_headers(tmp_path: Path, inbox_client):
    # The shell and the API both carry the baseline hardening headers.
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("<!doctype html><title>kew</title>")
    app = create_app(
        lambda: nullcontext(_EmptyStore()), repo_dir=tmp_path, static_dir=web
    )
    client = inbox_client(app, loopback_peer=True)
    for path in ("/", "/api/runs"):
        resp = client.get(path)
        assert resp.headers["x-content-type-options"] == "nosniff"
        assert resp.headers["x-frame-options"] == "DENY"
        assert "default-src 'self'" in resp.headers["content-security-policy"]
