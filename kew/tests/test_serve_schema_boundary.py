"""SchemaTooNewError in the per-request store factory returns 503, not 500."""

from pathlib import Path

from fastapi import HTTPException

from kew.links import UPGRADE_HINT
from kew.serve.app import create_app


def _boom_factory():
    raise HTTPException(
        status_code=503,
        detail=(
            "dispatch.db is schema v99; this kew supports up to v17. "
            f"Upgrade kew to open it.\nFix: {UPGRADE_HINT}"
        ),
    )


def test_schema_too_new_returns_503(tmp_path: Path, inbox_client):
    app = create_app(_boom_factory, repo_dir=tmp_path)
    client = inbox_client(app, loopback_peer=True)
    resp = client.get("/api/runs")
    assert resp.status_code == 503
    body = resp.json()
    assert "schema" in body["detail"].lower()
    assert "upgrade" in body["detail"].lower()


def test_schema_too_new_body_includes_versions(tmp_path: Path, inbox_client):
    app = create_app(_boom_factory, repo_dir=tmp_path)
    client = inbox_client(app, loopback_peer=True)
    resp = client.get("/api/runs")
    body = resp.json()
    assert "v99" in body["detail"]
    assert "v17" in body["detail"]
