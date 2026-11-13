from contextlib import nullcontext
from unittest.mock import patch

import pytest

from kew.serve.app import create_app
from kew.service.models import ApprovalResultDTO, FleetStopResultDTO

# `inbox_client` is the conftest fixture (factory).


class FakeStore:
    def list_all(self):  # for the existing read routes
        return []

    def get_fleet_stopped(self):
        return None

    def list_pending(self):
        return []


@pytest.fixture
def client(tmp_path, inbox_client):
    from kew.config import DispatchConfig
    from kew.serve.auth import SessionStore

    app = create_app(
        lambda: nullcontext(FakeStore()),
        repo_dir=tmp_path,
        config=DispatchConfig(),
        token="TESTTOKEN",
        sessions=SessionStore(),
        bind_host="127.0.0.1",
        bind_port=8765,
        expose=False,
    )
    # loopback_peer=True: reads open (local operator); writes still need a session.
    return inbox_client(app, loopback_peer=True)


def _login(client) -> dict:
    r = client.post("/api/auth/login", json={"token": "TESTTOKEN"})
    assert r.status_code == 200
    return {"X-Kew-CSRF": r.json()["csrf_token"]}


def test_login_bad_token_401(client):
    assert client.post("/api/auth/login", json={"token": "WRONG"}).status_code == 401


def test_login_rejects_token_in_query_only(client):
    # M2/C1: token must be body-only; a ?token= with empty body is not a login.
    r = client.post("/api/auth/login?token=TESTTOKEN", json={})
    assert r.status_code in (401, 422)


def test_login_sets_cookies_and_session_endpoint(client):
    _login(client)
    s = client.get("/api/auth/session")
    assert s.status_code == 200 and s.json()["authenticated"] is True


def test_approve_requires_auth(client):
    assert (
        client.post("/api/approvals/5", json={"action": "approve"}).status_code == 401
    )


def test_approve_maps_status_to_http(client):
    headers = _login(client)
    cases = {
        "released": 200,
        "denied": 200,
        "not_held": 409,
        "gh_error": 502,
        "unauthorized": 403,
    }
    for status, code in cases.items():
        with patch(
            "kew.serve.api_approvals.decide_hold",
            return_value=ApprovalResultDTO(status=status),
        ):
            r = client.post(
                "/api/approvals/5", json={"action": "approve"}, headers=headers
            )
            assert r.status_code == code, status


def test_approve_passes_source_web(client):
    headers = _login(client)
    with patch(
        "kew.serve.api_approvals.decide_hold",
        return_value=ApprovalResultDTO(status="released"),
    ) as m:
        client.post(
            "/api/approvals/5",
            json={"action": "approve", "reason": "go"},
            headers=headers,
        )
    assert m.call_args.kwargs["source"] == "web"
    assert m.call_args.args[0] == "approve"


def test_approve_accepts_string_github_id(client):
    # The frontend sends the durable item id as a string; a GitHub-numeric
    # string must coerce and reach decide_hold as the int issue_num.
    headers = _login(client)
    with patch(
        "kew.serve.api_approvals.decide_hold",
        return_value=ApprovalResultDTO(status="released"),
    ) as m:
        r = client.post("/api/approvals/5", json={"action": "approve"}, headers=headers)
    assert r.status_code == 200
    assert m.call_args.kwargs["issue_num"] == 5


def test_approve_non_numeric_item_is_404_not_422(client):
    # Non-GitHub approvals are the deferred wave: a tracker id like SFD-12 has
    # no int-keyed hold to decide — 404 with a clear message, never a 422 from
    # path-type coercion (the pre-fix behavior when the param was typed int).
    headers = _login(client)
    r = client.post(
        "/api/approvals/SFD-12", json={"action": "approve"}, headers=headers
    )
    assert r.status_code == 404
    assert "SFD-12" in r.json()["detail"]


def test_list_open_holds_read(client):
    with patch("kew.serve.api_approvals.list_open_holds", return_value=[]):
        assert client.get("/api/approvals").status_code == 200


def test_fleet_stop_requires_auth_and_wraps(client):
    assert client.post("/api/fleet/stop", json={}).status_code == 401
    headers = _login(client)
    with patch(
        "kew.serve.api_fleet.stop_fleet_service",
        return_value=FleetStopResultDTO(
            stopped=True, already_stopped=False, runs_killed=[]
        ),
    ):
        r = client.post("/api/fleet/stop", json={"reason": "halt"}, headers=headers)
    assert r.status_code == 200 and r.json()["stopped"] is True
