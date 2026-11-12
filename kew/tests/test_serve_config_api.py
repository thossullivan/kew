"""Tests for the config-manager write API (api_config.py, Phase 2 of #368/#369)."""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path

import pytest

from kew.audit_chain import CHAIN_FORMAT_ITEM_KEYS, ChainRow, entry_hash
from kew.config import ConfigError, load_config
from kew.run_store import AuditEvent, SqliteRunStore
from kew.serve.app import create_app
from kew.serve.auth import SessionStore


@contextmanager
def _store_factory(db_path: str):
    store = SqliteRunStore(db_path)
    try:
        yield store
    finally:
        store.close()


_TEST_CONFIG = load_config()


def _make_app(tmp_path: Path, *, expose: bool = False):
    db = str(tmp_path / "kew.db")
    SqliteRunStore(db).close()

    app = create_app(
        lambda: _store_factory(db),
        repo_dir=tmp_path,
        config=_TEST_CONFIG,
        token="TESTTOKEN",
        sessions=SessionStore(),
        bind_host="127.0.0.1",
        bind_port=8765,
        expose=expose,
    )
    return app, db


@pytest.fixture
def client(tmp_path, inbox_client):
    app, _db = _make_app(tmp_path)
    return inbox_client(app, loopback_peer=True)


@pytest.fixture
def client_and_store(tmp_path, inbox_client):
    app, db = _make_app(tmp_path)
    return inbox_client(app, loopback_peer=True), db


def _event_hash(ev: AuditEvent) -> str:
    raw_payload = json.dumps(ev.payload) if ev.payload is not None else None
    return entry_hash(
        ChainRow(
            id=ev.id,
            timestamp=ev.timestamp,
            event=ev.event,
            run_id=ev.run_id,
            issue_num=ev.issue_num,
            repo=ev.repo,
            actor_type=ev.actor_type,
            actor_id=ev.actor_id,
            endpoint=ev.endpoint,
            policy_hash=ev.policy_hash,
            payload=raw_payload,
            prev_hash=ev.prev_hash,
            item_id=ev.item_id,
            dedup_key=ev.dedup_key,
            # Every row written since the v77 step hashes in this format.
            chain_format=CHAIN_FORMAT_ITEM_KEYS,
        )
    )


def _login(client) -> dict[str, str]:
    r = client.post("/api/auth/login", json={"token": "TESTTOKEN"})
    assert r.status_code == 200
    return {"X-Kew-CSRF": r.json()["csrf_token"]}


# ── GET /api/config/budget ──────────────────────────────────────────


class TestGetBudget:
    def test_returns_budget_fields(self, client):
        r = client.get("/api/config/budget")
        assert r.status_code == 200
        data = r.json()
        expected = {
            "budget.per_issue_usd",
            "budget.daily_usd",
            "budget.weekly_usd",
        }
        assert set(data.keys()) == expected

    def test_each_field_has_provenance(self, client):
        r = client.get("/api/config/budget")
        for _field, info in r.json().items():
            assert "effective" in info
            assert "file_value" in info
            assert "override" in info

    def test_no_override_shows_null(self, client):
        r = client.get("/api/config/budget")
        for info in r.json().values():
            assert info["override"] is None

    def test_override_shows_metadata(self, client):
        headers = _login(client)
        client.post(
            "/api/config/budget.per_issue_usd",
            json={"value": 99.0},
            headers=headers,
        )
        r = client.get("/api/config/budget")
        info = r.json()["budget.per_issue_usd"]
        assert info["effective"] == 99.0
        assert info["override"]["value"] == 99.0
        assert info["override"]["set_by"] is not None

    def test_requires_session_when_exposed(self, tmp_path, inbox_client):
        app, _ = _make_app(tmp_path, expose=True)
        c = inbox_client(app, loopback_peer=False)
        assert c.get("/api/config/budget").status_code == 401


# ── GET /api/config/overridable ─────────────────────────────────────


_ALL_OVERRIDABLE = {
    "budget.per_issue_usd",
    "budget.daily_usd",
    "budget.weekly_usd",
    "agent.max_budget_usd",
    "agent.model",
    "review.model",
    "routing.default",
    "routing.simple",
    "routing.medium",
    "routing.complex",
    "breaker.cost_anomaly_factor",
}


class TestGetOverridable:
    def test_covers_all_allowlisted_fields_grouped_by_section(self, client):
        r = client.get("/api/config/overridable")
        assert r.status_code == 200
        grouped = r.json()
        # Grouped by section; flatten to compare against the allowlist.
        seen = {field for section in grouped.values() for field in section}
        assert seen == _ALL_OVERRIDABLE
        # Fields land under their own section key.
        assert "breaker.cost_anomaly_factor" in grouped["breaker"]
        assert "agent.model" in grouped["agent"]

    def test_each_field_carries_full_resolution_story(self, client):
        r = client.get("/api/config/overridable")
        for section in r.json().values():
            for info in section.values():
                assert "effective" in info
                assert "file_value" in info
                assert "default_value" in info
                assert "override" in info

    def test_default_value_from_defaults_layer(self, client):
        r = client.get("/api/config/overridable")
        info = r.json()["breaker"]["breaker.cost_anomaly_factor"]
        # Fresh-defaults layer, independent of any file/override/env.
        assert info["default_value"] == 3.0

    def test_no_override_shows_null(self, client):
        r = client.get("/api/config/overridable")
        for section in r.json().values():
            for info in section.values():
                assert info["override"] is None

    def test_override_layer_wins_and_shows_metadata(self, client):
        headers = _login(client)
        client.post(
            "/api/config/breaker.cost_anomaly_factor",
            json={"value": 6.0, "reason": "spendy epic"},
            headers=headers,
        )
        r = client.get("/api/config/overridable")
        info = r.json()["breaker"]["breaker.cost_anomaly_factor"]
        assert info["effective"] == 6.0
        assert info["default_value"] == 3.0
        assert info["override"]["value"] == 6.0
        assert info["override"]["reason"] == "spendy epic"
        assert info["override"]["set_by"] is not None
        assert info["override"]["set_at"] is not None

    def test_open_read_when_unexposed(self, tmp_path, inbox_client):
        app, _ = _make_app(tmp_path, expose=False)
        c = inbox_client(app, loopback_peer=True)
        assert c.get("/api/config/overridable").status_code == 200

    def test_requires_session_when_exposed(self, tmp_path, inbox_client):
        app, _ = _make_app(tmp_path, expose=True)
        c = inbox_client(app, loopback_peer=False)
        assert c.get("/api/config/overridable").status_code == 401

    def test_session_reads_when_exposed(self, tmp_path, inbox_client):
        app, _ = _make_app(tmp_path, expose=True)
        # tls=True: exposed mode sets Secure cookies, which a cookie jar only
        # replays over https — the browser's side of the TLS proxy (W1.6).
        c = inbox_client(app, loopback_peer=False, tls=True)
        _login(c)
        assert c.get("/api/config/overridable").status_code == 200


# ── breaker.cost_anomaly_factor through the generic write endpoint (#712) ──


class TestBreakerOverrideEndpoint:
    def test_set_valid_factor(self, client):
        headers = _login(client)
        r = client.post(
            "/api/config/breaker.cost_anomaly_factor",
            json={"value": 4.5},
            headers=headers,
        )
        assert r.status_code == 200
        assert r.json()["field"] == "breaker.cost_anomaly_factor"
        assert r.json()["effective"] == 4.5

    def test_below_one_rejected_400(self, client):
        headers = _login(client)
        r = client.post(
            "/api/config/breaker.cost_anomaly_factor",
            json={"value": 0.5},
            headers=headers,
        )
        assert r.status_code == 400

    def test_non_approver_denied_403(self, tmp_path, inbox_client, monkeypatch):
        monkeypatch.setenv("KEW_ACTOR", "mallory")
        app, db = _make_app_with_approvers(tmp_path, ["alice"])
        c = inbox_client(app, loopback_peer=True)
        headers = _login(c)
        r = c.post(
            "/api/config/breaker.cost_anomaly_factor",
            json={"value": 4.0},
            headers=headers,
        )
        assert r.status_code == 403
        store = SqliteRunStore(db)
        assert store.get_config_override("breaker.cost_anomaly_factor") is None
        store.close()


# ── POST /api/config/{field} ───────────────────────────────────────


class TestPostOverride:
    def test_config_error_from_service_is_translated_to_400(
        self, client, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        error = ConfigError(
            path=tmp_path / "kew.toml",
            key="[budget].per_issue_usd",
            expectation="a non-negative number",
            fix="use zero or a positive number",
            kind="invalid_value",
        )

        def fail_load_config():
            raise error

        monkeypatch.setattr("kew.service.config.load_config", fail_load_config)
        headers = _login(client)
        response = client.post(
            "/api/config/budget.per_issue_usd",
            json={"value": 10.0},
            headers=headers,
        )

        assert response.status_code == 400
        assert "[budget].per_issue_usd" in response.json()["detail"]

    def test_requires_auth(self, client):
        r = client.post("/api/config/budget.per_issue_usd", json={"value": 10.0})
        assert r.status_code == 401

    def test_requires_csrf(self, client):
        _login(client)
        r = client.post("/api/config/budget.per_issue_usd", json={"value": 10.0})
        assert r.status_code == 403

    def test_off_allowlist_field_400(self, client):
        headers = _login(client)
        r = client.post(
            "/api/config/security.enable_network",
            json={"value": True},
            headers=headers,
        )
        assert r.status_code == 400
        assert "not overridable" in r.json()["detail"]

    def test_invalid_value_400(self, client):
        headers = _login(client)
        r = client.post(
            "/api/config/budget.per_issue_usd",
            json={"value": -5.0},
            headers=headers,
        )
        assert r.status_code == 400

    def test_success_returns_effective(self, client):
        headers = _login(client)
        r = client.post(
            "/api/config/budget.per_issue_usd",
            json={"value": 42.0},
            headers=headers,
        )
        assert r.status_code == 200
        data = r.json()
        assert data["effective"] == 42.0
        assert data["field"] == "budget.per_issue_usd"

    def test_emits_audit_event(self, client_and_store):
        client, db = client_and_store
        headers = _login(client)
        client.post(
            "/api/config/budget.daily_usd",
            json={"value": 100.0, "reason": "need more budget"},
            headers=headers,
        )
        store = SqliteRunStore(db)
        events = store.list_audit_events(limit=1, event="config_override_set")
        assert len(events) == 1
        ev = events[0]
        assert ev.event == "config_override_set"
        assert ev.actor_type == "human"
        assert ev.payload is not None
        assert ev.payload["field"] == "budget.daily_usd"
        assert ev.payload["new_value"] == 100.0
        assert ev.payload["reason"] == "need more budget"
        store.close()

    def test_audit_event_cross_linked(self, client_and_store):
        client, db = client_and_store
        headers = _login(client)
        client.post(
            "/api/config/budget.per_issue_usd",
            json={"value": 25.0},
            headers=headers,
        )
        store = SqliteRunStore(db)
        override = store.get_config_override("budget.per_issue_usd")
        assert override is not None
        assert override["audit_event_id"] is not None
        events = store.list_audit_events(limit=1, event="config_override_set")
        assert override["audit_event_id"] == events[0].id
        store.close()

    def test_upsert_overwrites(self, client):
        headers = _login(client)
        client.post(
            "/api/config/budget.per_issue_usd",
            json={"value": 10.0},
            headers=headers,
        )
        r = client.post(
            "/api/config/budget.per_issue_usd",
            json={"value": 20.0},
            headers=headers,
        )
        assert r.status_code == 200
        assert r.json()["effective"] == 20.0

    def test_reason_is_optional(self, client):
        headers = _login(client)
        r = client.post(
            "/api/config/budget.per_issue_usd",
            json={"value": 10.0},
            headers=headers,
        )
        assert r.status_code == 200

    def test_hash_chained(self, client_and_store):
        client, db = client_and_store
        headers = _login(client)
        client.post(
            "/api/config/budget.per_issue_usd",
            json={"value": 10.0},
            headers=headers,
        )
        client.post(
            "/api/config/budget.daily_usd",
            json={"value": 75.0},
            headers=headers,
        )
        store = SqliteRunStore(db)
        events = store.list_audit_events(limit=2, event="config_override_set")
        assert len(events) == 2
        # list_audit_events returns newest-first; sort by id for chronological
        earlier, later = sorted(events, key=lambda e: e.id)
        assert earlier.prev_hash is not None
        assert later.prev_hash is not None
        assert later.prev_hash == _event_hash(earlier)
        store.close()


# ── DELETE /api/config/{field} ──────────────────────────────────────


class TestDeleteOverride:
    def test_requires_auth(self, client):
        assert client.delete("/api/config/budget.per_issue_usd").status_code == 401

    def test_requires_csrf(self, client):
        _login(client)
        assert client.delete("/api/config/budget.per_issue_usd").status_code == 403

    def test_reverts_to_file_value(self, client):
        headers = _login(client)
        client.post(
            "/api/config/budget.per_issue_usd",
            json={"value": 99.0},
            headers=headers,
        )
        r = client.delete("/api/config/budget.per_issue_usd", headers=headers)
        assert r.status_code == 200
        data = r.json()
        assert data["field"] == "budget.per_issue_usd"
        assert data["effective"] == _TEST_CONFIG.budget.per_issue_usd

    def test_delete_nonexistent_is_404(self, client):
        headers = _login(client)
        r = client.delete("/api/config/budget.per_issue_usd", headers=headers)
        assert r.status_code == 404

    def test_delete_nonexistent_no_audit_event(self, client_and_store):
        client, db = client_and_store
        headers = _login(client)
        client.delete("/api/config/budget.per_issue_usd", headers=headers)
        store = SqliteRunStore(db)
        events = store.list_audit_events(limit=10, event="config_override_reverted")
        assert len(events) == 0
        store.close()

    def test_off_allowlist_field_400(self, client):
        headers = _login(client)
        r = client.delete("/api/config/security.enable_network", headers=headers)
        assert r.status_code == 400

    def test_emits_revert_audit_event(self, client_and_store):
        client, db = client_and_store
        headers = _login(client)
        client.post(
            "/api/config/budget.per_issue_usd",
            json={"value": 50.0},
            headers=headers,
        )
        client.delete("/api/config/budget.per_issue_usd", headers=headers)
        store = SqliteRunStore(db)
        events = store.list_audit_events(limit=1, event="config_override_reverted")
        assert len(events) == 1
        ev = events[0]
        assert ev.payload is not None
        assert ev.payload["field"] == "budget.per_issue_usd"
        assert "reverted_to_file_value" in ev.payload
        store.close()


# ── Origin / Host guards (reuse approval patterns) ─────────────────


class TestOriginHostGuards:
    def test_bad_host_rejected(self, tmp_path, inbox_client):
        app, _ = _make_app(tmp_path)
        c = inbox_client(app, loopback_peer=True)
        headers = _login(c)
        r = c.post(
            "/api/config/budget.per_issue_usd",
            json={"value": 10.0},
            headers={**headers, "Host": "evil.com:8765"},
        )
        assert r.status_code == 403

    def test_bad_origin_rejected(self, tmp_path, inbox_client):
        app, _ = _make_app(tmp_path)
        c = inbox_client(app, loopback_peer=True)
        headers = _login(c)
        r = c.post(
            "/api/config/budget.per_issue_usd",
            json={"value": 10.0},
            headers={**headers, "Origin": "http://evil.com:8765"},
        )
        assert r.status_code == 403


# ── Routing/model overrides through the generic endpoint (#659) ──────


class TestRoutingModelOverride:
    """agent.model / routing.* flow through the existing generic endpoint."""

    def test_set_agent_model_override(self, client):
        headers = _login(client)
        r = client.post(
            "/api/config/agent.model",
            json={"value": "claude-opus-4-8", "reason": "opus race"},
            headers=headers,
        )
        assert r.status_code == 200
        data = r.json()
        assert data["field"] == "agent.model"
        assert data["effective"] == "claude-opus-4-8"

    def test_agent_model_override_audit_provenance(self, client_and_store):
        client, db = client_and_store
        headers = _login(client)
        client.post(
            "/api/config/agent.model",
            json={"value": "claude-opus-4-8", "reason": "opus race"},
            headers=headers,
        )
        store = SqliteRunStore(db)
        events = store.list_audit_events(limit=1, event="config_override_set")
        assert len(events) == 1
        ev = events[0]
        assert ev.actor_type == "human"
        assert ev.payload is not None
        assert ev.payload["field"] == "agent.model"
        assert ev.payload["new_value"] == "claude-opus-4-8"
        assert ev.payload["reason"] == "opus race"
        # Override row carries provenance (set_by, reason) for the banner.
        override = store.get_config_override("agent.model")
        assert override is not None
        assert override["set_by"] == "web"
        assert override["reason"] == "opus race"
        assert override["audit_event_id"] == ev.id
        store.close()

    def test_clear_agent_model_override(self, client_and_store):
        client, db = client_and_store
        headers = _login(client)
        client.post(
            "/api/config/agent.model",
            json={"value": "claude-opus-4-8"},
            headers=headers,
        )
        r = client.delete("/api/config/agent.model", headers=headers)
        assert r.status_code == 200
        assert r.json()["field"] == "agent.model"
        store = SqliteRunStore(db)
        assert store.get_config_override("agent.model") is None
        reverts = store.list_audit_events(limit=1, event="config_override_reverted")
        assert len(reverts) == 1
        assert reverts[0].payload["field"] == "agent.model"
        store.close()

    def test_routing_bare_string_rejected_with_runner_hint(self, client):
        headers = _login(client)
        r = client.post(
            "/api/config/routing.medium",
            json={"value": "claude-code"},
            headers=headers,
        )
        assert r.status_code == 400
        assert "runner-name" in r.json()["detail"]

    def test_set_routing_entry_override(self, client):
        headers = _login(client)
        r = client.post(
            "/api/config/routing.medium",
            json={"value": {"runner": "claude-code", "model": "claude-opus-4-8"}},
            headers=headers,
        )
        assert r.status_code == 200
        assert r.json()["field"] == "routing.medium"


# ── Approver gate + policy binding for spend-authorizing fields (#681) ──


def _make_app_with_approvers(tmp_path: Path, approvers: list[str]):
    db = str(tmp_path / "kew.db")
    SqliteRunStore(db).close()
    cfg = _TEST_CONFIG.model_copy(deep=True)
    cfg.approval.approvers = approvers
    app = create_app(
        lambda: _store_factory(db),
        repo_dir=tmp_path,
        config=cfg,
        token="TESTTOKEN",
        sessions=SessionStore(),
        bind_host="127.0.0.1",
        bind_port=8765,
        expose=False,
    )
    return app, db


class TestApproverGate:
    """Spend-authorizing overrides are gated on [approval].approvers (#681)."""

    def test_non_approver_denied_403(self, tmp_path, inbox_client, monkeypatch):
        monkeypatch.setenv("KEW_ACTOR", "mallory")
        app, db = _make_app_with_approvers(tmp_path, ["alice"])
        c = inbox_client(app, loopback_peer=True)
        headers = _login(c)
        r = c.post(
            "/api/config/budget.per_issue_usd",
            json={"value": 42.0},
            headers=headers,
        )
        assert r.status_code == 403
        # Denied write leaves no override row behind.
        store = SqliteRunStore(db)
        assert store.get_config_override("budget.per_issue_usd") is None
        store.close()

    def test_listed_approver_allowed_200_with_policy_hash(
        self, tmp_path, inbox_client, monkeypatch
    ):
        monkeypatch.setenv("KEW_ACTOR", "alice")
        app, db = _make_app_with_approvers(tmp_path, ["alice"])
        c = inbox_client(app, loopback_peer=True)
        headers = _login(c)
        r = c.post(
            "/api/config/agent.model",
            json={"value": "claude-opus-4-8"},
            headers=headers,
        )
        assert r.status_code == 200
        store = SqliteRunStore(db)
        ev = store.list_audit_events(limit=1, event="config_override_set")[0]
        assert ev.policy_hash is not None
        assert ev.payload["source"] == "web"
        assert ev.payload["actor"] == "alice"
        store.close()

    def test_non_approver_denied_revert_403(self, tmp_path, inbox_client, monkeypatch):
        monkeypatch.setenv("KEW_ACTOR", "alice")
        app, db = _make_app_with_approvers(tmp_path, ["alice"])
        c = inbox_client(app, loopback_peer=True)
        headers = _login(c)
        c.post(
            "/api/config/budget.per_issue_usd",
            json={"value": 42.0},
            headers=headers,
        )
        monkeypatch.setenv("KEW_ACTOR", "mallory")
        r = c.delete("/api/config/budget.per_issue_usd", headers=headers)
        assert r.status_code == 403
        store = SqliteRunStore(db)
        assert store.get_config_override("budget.per_issue_usd") is not None
        store.close()


class TestProvenanceReasonOptional:
    """Provenance is auto-composed; the human reason stays optional (#681)."""

    def test_empty_reason_still_records_provenance(self, client_and_store):
        client, db = client_and_store
        headers = _login(client)
        client.post(
            "/api/config/budget.per_issue_usd",
            json={"value": 12.0},  # no reason key
            headers=headers,
        )
        store = SqliteRunStore(db)
        ev = store.list_audit_events(limit=1, event="config_override_set")[0]
        assert ev.payload["reason"] == ""
        assert ev.payload["actor"]  # actor auto-composed
        assert ev.payload["source"] == "web"
        assert ev.payload["new_value"] == 12.0
        store.close()


# ── the human-review policy through the same surface (I13C) ─────────


def _review_app(tmp_path: Path, *, approvers: list[str] | None = None):
    """The config app bound to a canonical delivery repository."""
    db = str(tmp_path / "kew.db")
    SqliteRunStore(db).close()
    cfg = _TEST_CONFIG.model_copy(deep=True)
    cfg.project.repo = "acme/widgets"
    cfg.approval.approvers = approvers or []
    app = create_app(
        lambda: _store_factory(db),
        repo_dir=tmp_path,
        config=cfg,
        token="TESTTOKEN",
        sessions=SessionStore(),
        bind_host="127.0.0.1",
        bind_port=8765,
        expose=False,
    )
    return app, db, cfg


class TestHumanReviewConfigApi:
    """The API edits I13's durable policy — same values as the service."""

    _FIELD = "human_review.max_diff_lines"
    _URL = "/api/config/human_review.max_diff_lines"

    @pytest.fixture
    def review_client(self, tmp_path, inbox_client):
        app, db, cfg = _review_app(tmp_path)
        return inbox_client(app, loopback_peer=True), db, cfg

    def test_get_renders_the_system_policy(self, review_client):
        client, _db, _cfg = review_client
        body = client.get("/api/config/human_review").json()
        assert body["delivery_repo"] == "acme/widgets"
        assert body["values"] == {
            "human_review.max_pending_reviews": 3,
            "human_review.max_diff_lines": 500,
            "human_review.max_paths": 20,
        }
        assert body["generation"] == 0
        assert body["source"] == "system"
        assert body["expires_at"] is None
        assert body["pending_reversion"] is None
        assert body["over_cap"] is False
        assert body["pending_reviews"] == 0

    def test_get_requires_a_read_guard(self, tmp_path, inbox_client):
        app, _db, _cfg = _review_app(tmp_path)
        remote = inbox_client(app, loopback_peer=False)
        assert remote.get("/api/config/human_review").status_code in (401, 403)

    def test_post_requires_auth_and_csrf(self, review_client):
        client, _db, _cfg = review_client
        assert client.post(self._URL, json={"value": 200}).status_code == 401
        _login(client)
        assert client.post(self._URL, json={"value": 200}).status_code == 403

    def test_post_lowers_and_reports_the_new_generation(self, review_client):
        client, _db, _cfg = review_client
        headers = _login(client)
        r = client.post(
            self._URL, json={"value": 200, "reason": "release week"}, headers=headers
        )
        assert r.status_code == 200
        body = r.json()
        assert body["field"] == self._FIELD
        assert body["effective"] == 200
        assert body["generation"] == 1
        assert body["source"] == "override"
        assert client.get("/api/config/human_review").json()["values"][self._FIELD] == (
            200
        )

    def test_post_accepts_a_utc_expiry_and_renders_the_pending_reversion(
        self, review_client
    ):
        client, _db, _cfg = review_client
        headers = _login(client)
        expiry = "2099-01-01T00:00:00+00:00"
        body = client.post(
            self._URL, json={"value": 200, "expires_at": expiry}, headers=headers
        ).json()
        assert body["expires_at"] == expiry
        assert body["pending_reversion"]["at"] == expiry
        assert body["pending_reversion"]["to"][self._FIELD] == 500

    def test_post_rejects_a_naive_expiry(self, review_client):
        client, _db, _cfg = review_client
        headers = _login(client)
        r = client.post(
            self._URL,
            json={"value": 200, "expires_at": "2099-01-01T00:00:00"},
            headers=headers,
        )
        assert r.status_code == 400

    def test_post_rejects_an_unusable_value(self, review_client):
        client, _db, _cfg = review_client
        headers = _login(client)
        r = client.post(self._URL, json={"value": -1}, headers=headers)
        assert r.status_code == 400

    def test_a_stale_generation_is_a_conflict(self, review_client):
        client, _db, _cfg = review_client
        headers = _login(client)
        client.post(self._URL, json={"value": 200}, headers=headers)
        r = client.post(
            self._URL, json={"value": 10, "if_generation": 0}, headers=headers
        )
        assert r.status_code == 409
        assert client.get("/api/config/human_review").json()["values"][self._FIELD] == (
            200
        )

    def test_expiry_and_generation_are_rejected_for_ordinary_fields(
        self, review_client
    ):
        client, _db, _cfg = review_client
        headers = _login(client)
        r = client.post(
            "/api/config/budget.per_issue_usd",
            json={"value": 10.0, "expires_at": "2099-01-01T00:00:00+00:00"},
            headers=headers,
        )
        assert r.status_code == 400

    def test_delete_reverts_the_whole_policy(self, review_client):
        client, _db, _cfg = review_client
        headers = _login(client)
        client.post(self._URL, json={"value": 200}, headers=headers)
        r = client.delete("/api/config/human_review", headers=headers)
        assert r.status_code == 200
        assert r.json()["values"][self._FIELD] == 500
        assert r.json()["source"] == "system"
        again = client.delete("/api/config/human_review", headers=headers)
        assert again.status_code == 404

    def test_delete_reverts_one_field(self, review_client):
        client, _db, _cfg = review_client
        headers = _login(client)
        client.post(self._URL, json={"value": 200}, headers=headers)
        client.post(
            "/api/config/human_review.max_paths", json={"value": 5}, headers=headers
        )
        body = client.delete(self._URL, headers=headers).json()
        assert body["values"][self._FIELD] == 500
        assert body["values"]["human_review.max_paths"] == 5
        assert body["source"] == "override"

    def test_a_blocked_actor_is_denied(self, tmp_path, inbox_client, monkeypatch):
        monkeypatch.setenv("KEW_ACTOR", "mallory")
        app, db, _cfg = _review_app(tmp_path, approvers=["alice"])
        c = inbox_client(app, loopback_peer=True)
        headers = _login(c)
        assert c.post(self._URL, json={"value": 1}, headers=headers).status_code == 403
        with SqliteRunStore(db) as store:
            assert store.get_human_review_policy("acme/widgets") is None

    def test_the_api_and_the_service_render_identical_values(
        self, tmp_path, inbox_client
    ):
        from kew.service.config import human_review_state

        app, db, cfg = _review_app(tmp_path)
        client = inbox_client(app, loopback_peer=True)
        headers = _login(client)
        client.post(self._URL, json={"value": 200}, headers=headers)

        body = client.get("/api/config/human_review").json()
        with SqliteRunStore(db) as store:
            state = human_review_state(store, config=cfg)
        assert body == state.as_dict()

    def test_a_config_without_a_delivery_repo_is_a_400(self, tmp_path, inbox_client):
        app, _db = _make_app(tmp_path)  # _TEST_CONFIG has no project.repo
        client = inbox_client(app, loopback_peer=True)
        assert client.get("/api/config/human_review").status_code == 400


class TestFileValueExcludesTheEnvironmentLayer:
    """``file_value`` is the config FILE's value, not whatever the env says (#2298).

    ``load_config()`` applies the ``KEW_*`` layer, so with ``KEW_MAX_BUDGET``
    exported the console showed the env number as the file's value AND the DB
    override beside it as though that override were in force — when env outranks
    DB, so it was not. The one surface whose job is to say which layer wins was
    describing a web override that changed nothing.
    """

    def test_budget_file_value_ignores_an_env_override(
        self, tmp_path: Path, inbox_client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        toml = tmp_path / "kew.toml"
        toml.write_text("[agent]\nmax_budget_usd = 3.0\n")
        monkeypatch.setenv("KEW_PROJECT", str(tmp_path))
        monkeypatch.setenv("KEW_MAX_BUDGET", "99.0")
        app, _db = _make_app(tmp_path)
        client = inbox_client(app, loopback_peer=True)
        body = client.get("/api/config/overridable").json()
        entry = body["agent"]["agent.max_budget_usd"]
        assert entry["file_value"] == 3.0
        assert entry["effective"] == 99.0

    def test_budget_view_file_value_ignores_an_env_override(
        self, tmp_path: Path, inbox_client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        toml = tmp_path / "kew.toml"
        toml.write_text("[budget]\ndaily_usd = 5.0\n")
        monkeypatch.setenv("KEW_PROJECT", str(tmp_path))
        monkeypatch.setenv("KEW_MAX_BUDGET", "99.0")
        app, _db = _make_app(tmp_path)
        client = inbox_client(app, loopback_peer=True)
        body = client.get("/api/config/budget").json()
        assert body["budget.daily_usd"]["file_value"] == 5.0
