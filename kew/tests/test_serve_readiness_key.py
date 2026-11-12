"""A passive read never creates the readiness HMAC key (#2564).

``kew serve`` opens a WRITABLE store, so choosing "create or read" by store
mode let ``GET /api/dispatch/resolve`` write the operator's secret to disk the
first time an inherited proxy variable was digested. Creation is now chosen by
the operation: only the live confirm (the spend path) may create the key.

Every test here runs against a real, writable ``SqliteRunStore`` — a stub
store would bypass the very method that chose to create — and asserts the
EFFECT: whether a file exists under the key's directory.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from kew.config import AgentConfig, DispatchConfig, SandboxConfig
from kew.probe_route import resolve_probe_route
from kew.run_store import SqliteRunStore
from kew.serve.app import create_app
from kew.service.health import HealthProbes, HealthReader


@pytest.fixture
def key_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The issue's defeating input: an absent key path and an inherited proxy."""
    path = tmp_path / "keydir" / "readiness-hmac.key"
    monkeypatch.setenv("KEW_READINESS_HMAC_KEY_PATH", str(path))
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")
    return path


def _config() -> DispatchConfig:
    config = DispatchConfig(
        agent=AgentConfig(model="claude-opus-4-8"),
        sandbox=SandboxConfig(enabled=False, dangerously_skip_containment=True),
    )
    config.sandbox.proxy_environment = "inherit"
    return config


def _mk_app(tmp_path: Path) -> Any:
    db = str(tmp_path / "dispatch.db")

    @contextmanager
    def factory():
        # Writable, exactly as ``kew serve`` opens it.
        with SqliteRunStore(db, project_root=tmp_path) as store:
            yield store

    probes = HealthProbes(
        runtime_present=lambda _name: True,
        image_present=lambda _runtime, _ref: True,
        env_set=lambda name: name == "ANTHROPIC_API_KEY",
    )
    return create_app(
        factory,
        repo_dir=tmp_path,
        config=_config(),
        health_reader=HealthReader(probes=probes),
    )


def test_the_resolve_route_does_not_create_the_key(
    tmp_path: Path, key_path: Path, inbox_client
):
    """AC1: the request answers 200 and leaves no key behind."""
    client = inbox_client(_mk_app(tmp_path), loopback_peer=True)
    response = client.get("/api/dispatch/resolve?item=2564&complexity=complex")
    assert response.status_code == 200, response.text
    assert not key_path.exists()
    assert not key_path.parent.exists()


def test_the_digest_is_still_taken_on_a_passive_read(tmp_path: Path, key_path: Path):
    """The guard above can fail: the defeating input really reaches the key.

    The inherited proxy variable is digested on this route, so a passive read
    asks for the key — and without one, answers with the unbound preview key
    (not verified) instead of raising.
    """
    with SqliteRunStore(str(tmp_path / "dispatch.db")) as store:
        route = resolve_probe_route(_config(), complexity="complex", store=store)
    assert "HTTPS_PROXY" in route.inherited_proxy_names
    assert route.inherited_proxy_digest
    assert not key_path.parent.exists()


def test_the_confirmation_path_still_creates_the_key(tmp_path: Path, key_path: Path):
    """AC2: the live confirm — the one spend path — creates the key if absent."""
    with SqliteRunStore(str(tmp_path / "dispatch.db")) as store:
        route = resolve_probe_route(
            _config(), complexity="complex", store=store, create_key=True
        )
        key = store.readiness_proxy_key(create=False)
    assert key_path.is_file()
    assert key_path.read_text() == key
    assert key_path.stat().st_mode & 0o777 == 0o600
    assert route.inherited_proxy_digest


def test_the_live_confirm_asks_to_create_the_key(
    tmp_path: Path, key_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """AC2, at the caller: ``confirm_route_live`` resolves with ``create_key``."""
    from kew import readiness_probe

    seen: list[bool] = []

    class Stop(Exception):
        pass

    def resolving(*_args: Any, create_key: bool = False, **_kwargs: Any) -> Any:
        seen.append(create_key)
        raise Stop

    monkeypatch.setattr(readiness_probe, "resolve_probe_route", resolving)
    with (
        SqliteRunStore(str(tmp_path / "dispatch.db")) as store,
        pytest.raises(Stop),
    ):
        readiness_probe.confirm_route_live(store, _config(), complexity="complex")
    assert seen == [True]


#: Every GET route under ``/api`` that reads readiness: the dispatch plan (each
#: lane resolves a probe route and digests the proxy posture), the work
#: projections that look readiness up, and the runs views that report it.
READINESS_READS = [
    "/api/dispatch/resolve",
    "/api/dispatch/resolve?item=2564&complexity=simple",
    "/api/dispatch/resolve?item=2564&complexity=medium&lane=review",
    "/api/dispatch/resolve?item=2564&complexity=complex&lane=remediation",
    "/api/dashboard/work",
    "/api/verbs/work/2564",
    "/api/runs",
    "/api/health",
]


@pytest.mark.parametrize("route", READINESS_READS)
def test_no_readiness_read_creates_a_file_under_the_key_directory(
    tmp_path: Path, key_path: Path, inbox_client, route: str
):
    """AC3: nothing appears under the key directory, even when it exists."""
    key_path.parent.mkdir(mode=0o700)
    client = inbox_client(_mk_app(tmp_path), loopback_peer=True)
    response = client.get(route)
    assert response.status_code < 500, response.text
    assert list(key_path.parent.iterdir()) == []
