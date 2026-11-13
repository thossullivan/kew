"""Web-bundle presence/freshness introspection + the missing-build hint page.

Covers `kew.serve.bundle` (stamp read, staleness, warnings, hint page) and the
app wiring: a missing bundle serves a hint page instead of a bare 404, and the
`/api/bundle` endpoint reports the deployed stamp.
"""

from __future__ import annotations

import json
from contextlib import nullcontext
from pathlib import Path

from kew.serve.app import create_app
from kew.serve.bundle import (
    BundleStatus,
    evaluate_bundle,
    missing_build_html,
    read_stamp,
    startup_warnings,
)


class _EmptyStore:
    def list_all(self):
        return []


def _stamped_bundle(tmp_path: Path, *, git_sha: str, built_at: float) -> Path:
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("<!doctype html><title>kew</title>")
    (web / "build-stamp.json").write_text(
        json.dumps({"git_sha": git_sha, "built_at": built_at})
    )
    return web


# --- pure helpers -----------------------------------------------------------


def test_read_stamp_missing_returns_none(tmp_path: Path) -> None:
    (tmp_path / "web").mkdir()
    assert read_stamp(tmp_path / "web") is None
    assert read_stamp(None) is None


def test_read_stamp_invalid_json_returns_none(tmp_path: Path) -> None:
    web = tmp_path / "web"
    web.mkdir()
    (web / "build-stamp.json").write_text("{ not json")
    assert read_stamp(web) is None


def test_evaluate_bundle_absent(tmp_path: Path) -> None:
    status = evaluate_bundle(None)
    assert status == BundleStatus(
        present=False, git_sha="", built_at=0.0, source_mtime=0.0, stale=False
    )


def test_evaluate_bundle_present_and_fresh(tmp_path: Path) -> None:
    web = _stamped_bundle(tmp_path, git_sha="abc1234", built_at=9_000_000_000.0)
    src = tmp_path / "frontend"
    src.mkdir()
    (src / "app.ts").write_text("export const x = 1;")  # mtime ~ now << built_at
    status = evaluate_bundle(web, src)
    assert status.present is True
    assert status.git_sha == "abc1234"
    assert status.stale is False


def test_evaluate_bundle_stale_when_source_newer(tmp_path: Path) -> None:
    # Bundle built long ago; source file mtime is "now" → newer → stale.
    web = _stamped_bundle(tmp_path, git_sha="old0001", built_at=1_000_000.0)
    src = tmp_path / "frontend"
    src.mkdir()
    (src / "app.ts").write_text("export const x = 1;")
    status = evaluate_bundle(web, src)
    assert status.stale is True


def test_evaluate_bundle_no_source_never_stale(tmp_path: Path) -> None:
    # Wheel install: bundle present + stamped, but no frontend/ tree.
    web = _stamped_bundle(tmp_path, git_sha="deadbee", built_at=1_000_000.0)
    status = evaluate_bundle(web, tmp_path / "does-not-exist")
    assert status.present is True
    assert status.stale is False


def test_startup_warnings_absent_and_stale() -> None:
    absent = BundleStatus(False, "", 0.0, 0.0, False)
    assert any("bundle missing" in w for w in startup_warnings(absent))

    stale = BundleStatus(True, "old", 1.0, 2.0, True)
    assert any("stale" in w for w in startup_warnings(stale))

    fresh = BundleStatus(True, "new", 2.0, 1.0, False)
    assert startup_warnings(fresh) == []


def test_missing_build_html_mentions_build_command() -> None:
    html = missing_build_html()
    assert "build_web.sh" in html
    assert "src/kew/serve/web" in html


# --- app wiring -------------------------------------------------------------


def test_missing_bundle_serves_hint_page(tmp_path: Path, inbox_client) -> None:
    app = create_app(lambda: nullcontext(_EmptyStore()), repo_dir=tmp_path)
    client = inbox_client(app, loopback_peer=True)

    root = client.get("/")
    assert root.status_code == 200
    assert "text/html" in root.headers["content-type"]
    assert "not built" in root.text
    assert "build_web.sh" in root.text

    # A deep link gets the hint too (not a bare 404).
    deep = client.get("/epics/anything")
    assert deep.status_code == 200
    assert "not built" in deep.text


def test_missing_bundle_keeps_api_json_404(tmp_path: Path, inbox_client) -> None:
    app = create_app(lambda: nullcontext(_EmptyStore()), repo_dir=tmp_path)
    client = inbox_client(app, loopback_peer=True)

    assert client.get("/api/runs").status_code == 200
    unmatched = client.get("/api/definitely-not-a-route")
    assert unmatched.status_code == 404
    assert "text/html" not in unmatched.headers["content-type"]

    # The bare `/api` path is an API path too — a JSON 404, not the HTML hint.
    bare = client.get("/api")
    assert bare.status_code == 404
    assert "text/html" not in bare.headers["content-type"]


def test_bundle_endpoint_reports_absent(tmp_path: Path, inbox_client) -> None:
    app = create_app(lambda: nullcontext(_EmptyStore()), repo_dir=tmp_path)
    client = inbox_client(app, loopback_peer=True)
    body = client.get("/api/bundle").json()
    assert body["present"] is False
    assert body["git_sha"] == ""


def test_bundle_endpoint_reports_stamp(tmp_path: Path, inbox_client) -> None:
    web = _stamped_bundle(tmp_path, git_sha="cafe123", built_at=9_000_000_000.0)
    app = create_app(
        lambda: nullcontext(_EmptyStore()), repo_dir=tmp_path, static_dir=web
    )
    client = inbox_client(app, loopback_peer=True)
    body = client.get("/api/bundle").json()
    assert body["present"] is True
    assert body["git_sha"] == "cafe123"
    assert body["stale"] is False
