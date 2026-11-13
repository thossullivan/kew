"""SPA fallback for client-side routed deep links (#454 Phase B).

`/epics/{ref}` and `/loops/{id}` are client-side routes with no server-side
page; unknown GET paths outside /api must serve the shell
(index.html, cache-control: no-cache) so a hard refresh / shared link works.
`/api` 404s must stay JSON — the SPA fallback must never swallow them.
"""

from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path

from kew.serve.app import create_app


class _EmptyStore:
    def list_all(self):
        return []

    def list_epic_refs(self):
        return []


def _mk_app(tmp_path: Path):
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("<!doctype html><title>kew</title>")
    (web / "app.css").write_text("body{}")
    return create_app(
        lambda: nullcontext(_EmptyStore()), repo_dir=tmp_path, static_dir=web
    )


def test_epics_deep_link_serves_shell(tmp_path: Path, inbox_client) -> None:
    app = _mk_app(tmp_path)
    client = inbox_client(app, loopback_peer=True)
    r = client.get("/epics/anything")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert r.headers["cache-control"] == "no-cache"
    assert "<title>kew</title>" in r.text


def test_loops_deep_link_repaired(tmp_path: Path, inbox_client) -> None:
    app = _mk_app(tmp_path)
    client = inbox_client(app, loopback_peer=True)
    r = client.get("/loops/some-id")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


def test_api_404_is_json_never_html(tmp_path: Path, inbox_client) -> None:
    app = _mk_app(tmp_path)
    client = inbox_client(app, loopback_peer=True)

    known_route_404 = client.get("/api/epics/nonexistent")
    assert known_route_404.status_code == 404
    assert "application/json" in known_route_404.headers["content-type"]

    unmatched = client.get("/api/definitely-not-a-route")
    assert unmatched.status_code == 404
    assert "text/html" not in unmatched.headers["content-type"]
    assert "application/json" in unmatched.headers["content-type"]


def test_non_get_never_falls_back(tmp_path: Path, inbox_client) -> None:
    app = _mk_app(tmp_path)
    client = inbox_client(app, loopback_peer=True)
    r = client.post("/epics/anything")
    assert r.status_code in (404, 405)
    assert "text/html" not in r.headers["content-type"]


def test_root_and_asset_still_served(tmp_path: Path, inbox_client) -> None:
    app = _mk_app(tmp_path)
    client = inbox_client(app, loopback_peer=True)
    assert client.get("/").status_code == 200
    css = client.get("/app.css")
    assert css.status_code == 200
    assert (
        "cache-control" not in css.headers
        or css.headers.get("cache-control") != "no-cache"
    )
