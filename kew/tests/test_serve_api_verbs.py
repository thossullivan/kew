"""Tests for the verb write API (api_verbs.py, Group 4 of #684).

The four operator verbs — resume, epic-retry, queue, orphan-ack — are exposed as
authenticated POST endpoints behind ``require_write``; queue, resume and
retry are additionally gated on ``[approval].approvers`` and require an explicit
``confirm: true``. Each response returns the target's new authoritative state
(fleet resumed, node re-admitted, ready-labeled issue, orphan acked_at), and
queue/resume carry the epic-driver staleness flag. Every call emits its chained
audit event. ``--expose`` never loosens write-gating.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from kew.config import load_config
from kew.run_store import AdmissionIdentity, RunRecord, SqliteRunStore
from kew.serve.app import create_app
from kew.serve.auth import SessionStore

_TEST_CONFIG = load_config()


@contextmanager
def _store_factory(db_path: str) -> Any:
    store = SqliteRunStore(db_path)
    try:
        yield store
    finally:
        store.close()


def _make_app(
    tmp_path: Path,
    *,
    expose: bool = False,
    approvers: list[str] | None = None,
    timeout_minutes: int | None = None,
):
    db = str(tmp_path / "kew.db")
    SqliteRunStore(db).close()
    cfg = _TEST_CONFIG.model_copy(deep=True)
    cfg.project.repo = "o/r"
    if approvers is not None:
        cfg.approval.approvers = approvers
    if timeout_minutes is not None:
        cfg.agent.timeout_minutes = timeout_minutes
    app = create_app(
        lambda: _store_factory(db),
        repo_dir=tmp_path,
        config=cfg,
        token="TESTTOKEN",
        sessions=SessionStore(),
        bind_host="127.0.0.1",
        bind_port=8765,
        expose=expose,
    )
    return app, db


def _login(client) -> dict[str, str]:
    r = client.post("/api/auth/login", json={"token": "TESTTOKEN"})
    assert r.status_code == 200
    return {"X-Kew-CSRF": r.json()["csrf_token"]}


def _stop_fleet(db: str) -> None:
    store = SqliteRunStore(db)
    store.set_fleet_stopped(reason="test")
    store.close()


def _seed_orphan(db: str, pr_number: int = 7) -> None:
    store = SqliteRunStore(db)
    store.sync_orphan_prs(
        [(pr_number, f"https://x/pull/{pr_number}", "agent/issue-1", "no-record")]
    )
    store.close()


def _seed_reservation(db: str, epic_ref: str = "epic:x", issue: int = 12) -> None:
    store = SqliteRunStore(db)
    store.reserve_epic_node(epic_ref, issue, "run-a")
    store.close()


def _seed_live_driver(db: str, epic_ref: str = "epic:x") -> None:
    now = datetime.now(UTC).isoformat()
    store = SqliteRunStore(db)
    store.begin_epic_tick(epic_ref, now)
    store.finish_epic_tick(
        epic_ref,
        tick_ts=now,
        duration_s=1.0,
        interval_s=60.0,
        signal="ok",
        tick_id=None,
    )
    store.close()


def _audit_events(db: str, event: str) -> list[Any]:
    store = SqliteRunStore(db)
    try:
        return store.list_audit_events(event=event)
    finally:
        store.close()


@pytest.fixture
def client(tmp_path, inbox_client):
    app, db = _make_app(tmp_path)
    return inbox_client(app, loopback_peer=True), db


# ── auth matrix: writes need session + CSRF ─────────────────────────────


class TestAuthMatrix:
    def test_resume_unauth_401(self, client):
        c, _db = client
        assert c.post("/api/verbs/resume", json={"confirm": True}).status_code == 401

    def test_resume_session_no_csrf_403(self, client):
        c, _db = client
        c.post("/api/auth/login", json={"token": "TESTTOKEN"})
        assert c.post("/api/verbs/resume", json={"confirm": True}).status_code == 403

    def test_queue_unauth_401(self, client):
        c, _db = client
        assert c.post("/api/verbs/queue/42", json={"confirm": True}).status_code == 401

    def test_queue_session_no_csrf_403(self, client):
        c, _db = client
        c.post("/api/auth/login", json={"token": "TESTTOKEN"})
        assert c.post("/api/verbs/queue/42", json={"confirm": True}).status_code == 403

    def test_retry_unauth_401(self, client):
        c, _db = client
        assert c.post("/api/verbs/epics/epic:x/retry/12").status_code == 401

    def test_retry_session_no_csrf_403(self, client):
        c, _db = client
        c.post("/api/auth/login", json={"token": "TESTTOKEN"})
        assert c.post("/api/verbs/epics/epic:x/retry/12").status_code == 403

    def test_ack_unauth_401(self, client):
        c, _db = client
        assert c.post("/api/verbs/orphans/7/ack").status_code == 401

    def test_ack_session_no_csrf_403(self, client):
        c, _db = client
        c.post("/api/auth/login", json={"token": "TESTTOKEN"})
        assert c.post("/api/verbs/orphans/7/ack").status_code == 403


# ── approver gate + explicit confirm (queue + resume) ───────────────────


class TestApproverGateAndConfirm:
    def test_resume_missing_confirm_400(self, client):
        c, db = client
        _stop_fleet(db)
        headers = _login(c)
        assert c.post("/api/verbs/resume", json={}, headers=headers).status_code == 400

    def test_queue_missing_confirm_400(self, client):
        c, _db = client
        headers = _login(c)
        assert (
            c.post("/api/verbs/queue/42", json={}, headers=headers).status_code == 400
        )

    def test_resume_non_approver_403(self, tmp_path, inbox_client, monkeypatch):
        monkeypatch.setenv("KEW_ACTOR", "mallory")
        app, db = _make_app(tmp_path, approvers=["alice"])
        c = inbox_client(app, loopback_peer=True)
        _stop_fleet(db)
        headers = _login(c)
        r = c.post("/api/verbs/resume", json={"confirm": True}, headers=headers)
        assert r.status_code == 403
        # Denied: the fleet-stop latch is untouched.
        store = SqliteRunStore(db)
        assert store.get_fleet_stopped() is not None
        store.close()

    def test_queue_non_approver_403(self, tmp_path, inbox_client, monkeypatch):
        monkeypatch.setenv("KEW_ACTOR", "mallory")
        app, db = _make_app(tmp_path, approvers=["alice"])
        c = inbox_client(app, loopback_peer=True)
        headers = _login(c)
        with patch("kew.service.verbs.run") as gh_list:
            gh_list.return_value = MagicMock(
                returncode=0, stdout=json.dumps([{"number": 42}]), stderr=""
            )
            r = c.post("/api/verbs/queue/42", json={"confirm": True}, headers=headers)
        assert r.status_code == 403
        assert _audit_events(db, "issue_queued") == []

    def test_listed_approver_resume_200(self, tmp_path, inbox_client, monkeypatch):
        monkeypatch.setenv("KEW_ACTOR", "alice")
        app, db = _make_app(tmp_path, approvers=["alice"])
        c = inbox_client(app, loopback_peer=True)
        _stop_fleet(db)
        headers = _login(c)
        r = c.post("/api/verbs/resume", json={"confirm": True}, headers=headers)
        assert r.status_code == 200


# ── response state + audit events ───────────────────────────────────────


class TestResumeVerb:
    def test_resume_returns_state_and_driver_stale_and_emits(self, client):
        c, db = client
        _stop_fleet(db)
        headers = _login(c)
        r = c.post("/api/verbs/resume", json={"confirm": True}, headers=headers)
        assert r.status_code == 200
        body = r.json()
        assert body["resumed"] is True
        # No epic driver known → stale ("no live driver").
        assert body["driver_stale"] is True
        # Latch cleared server-side.
        store = SqliteRunStore(db)
        assert store.get_fleet_stopped() is None
        store.close()
        assert len(_audit_events(db, "fleet_resume")) == 1

    def test_resume_driver_stale_false_when_live(self, client):
        c, db = client
        _stop_fleet(db)
        _seed_live_driver(db)
        headers = _login(c)
        r = c.post("/api/verbs/resume", json={"confirm": True}, headers=headers)
        assert r.json()["driver_stale"] is False


class TestQueueVerb:
    def test_queue_marks_ready_returns_state_and_emits(self, client):
        c, db = client
        headers = _login(c)
        with (
            patch("kew.service.verbs.run") as gh_list,
            patch("kew.labels.run") as gh_edit,
        ):
            gh_list.return_value = MagicMock(
                returncode=0, stdout=json.dumps([{"number": 42}]), stderr=""
            )
            gh_edit.return_value = MagicMock(returncode=0, stdout="", stderr="")
            r = c.post("/api/verbs/queue/42", json={"confirm": True}, headers=headers)
        assert r.status_code == 200
        body = r.json()
        assert body["issue"] == 42
        assert body["ready"] is True
        assert body["driver_stale"] is True
        events = _audit_events(db, "issue_queued")
        assert len(events) == 1 and events[0].issue_num == 42
        gh_edit.assert_called_once()

    def test_queue_unknown_issue_404(self, client):
        c, db = client
        headers = _login(c)
        with patch("kew.service.verbs.run") as gh_list:
            gh_list.return_value = MagicMock(
                returncode=0, stdout=json.dumps([{"number": 42}]), stderr=""
            )
            r = c.post("/api/verbs/queue/99", json={"confirm": True}, headers=headers)
        assert r.status_code == 404
        assert _audit_events(db, "issue_queued") == []


def _epic_retry_items(db: str, epic_ref: str = "epic:x") -> list[str]:
    store = SqliteRunStore(db)
    try:
        return [row.item_id for row in store.list_epic_retries(epic_ref)]
    finally:
        store.close()


class TestRetryGate:
    """#2546: retry re-admits a node for new spend, so it is gated like queue."""

    @pytest.mark.parametrize("payload", [None, {}, {"confirm": False}])
    def test_retry_without_confirm_400_writes_nothing(self, client, payload):
        c, db = client
        _seed_reservation(db, "epic:x", 12)
        headers = _login(c)
        with patch("kew.labels.run") as gh:
            gh.return_value = MagicMock(returncode=0, stdout="", stderr="")
            r = c.post(
                "/api/verbs/epics/epic:x/retry/12", json=payload, headers=headers
            )
        # A bare POST (the pre-#2546 client) is the same 400 as an explicit
        # body without ``confirm: true`` — never a schema 422 — and none writes.
        assert r.status_code == 400
        assert "confirm" in r.json()["detail"]
        gh.assert_not_called()
        assert _epic_retry_items(db) == []
        assert _audit_events(db, "epic_node_retry") == []

    def test_retry_non_approver_403_writes_nothing(
        self, tmp_path, inbox_client, monkeypatch
    ):
        monkeypatch.setenv("KEW_ACTOR", "mallory")
        app, db = _make_app(tmp_path, approvers=["alice"])
        c = inbox_client(app, loopback_peer=True)
        _seed_reservation(db, "epic:x", 12)
        headers = _login(c)
        with patch("kew.labels.run") as gh:
            gh.return_value = MagicMock(returncode=0, stdout="", stderr="")
            r = c.post(
                "/api/verbs/epics/epic:x/retry/12",
                json={"confirm": True},
                headers=headers,
            )
        assert r.status_code == 403
        assert "mallory" in r.json()["detail"]
        gh.assert_not_called()
        assert _epic_retry_items(db) == []
        assert _audit_events(db, "epic_node_retry") == []
        store = SqliteRunStore(db)
        assert [row.run_id for row in store.list_epic_reservations("epic:x")] == [
            "run-a"
        ]
        store.close()

    def test_listed_approver_confirmed_retry_200(
        self, tmp_path, inbox_client, monkeypatch
    ):
        monkeypatch.setenv("KEW_ACTOR", "alice")
        app, db = _make_app(tmp_path, approvers=["alice"])
        c = inbox_client(app, loopback_peer=True)
        headers = _login(c)
        with patch("kew.labels.run") as gh:
            gh.return_value = MagicMock(returncode=0, stdout="", stderr="")
            r = c.post(
                "/api/verbs/epics/epic:x/retry/12",
                json={"confirm": True},
                headers=headers,
            )
        assert r.status_code == 200
        assert r.json() == {
            "epic_ref": "epic:x",
            "issue": 12,
            "item_id": "12",
            "re_admitted": True,
        }
        gh.assert_called()
        assert _epic_retry_items(db) == ["12"]
        events = _audit_events(db, "epic_node_retry")
        assert len(events) == 1 and events[0].payload["node"] == "12"


class TestRetryVerb:
    def test_retry_readmits_node_returns_state_and_emits(self, client):
        c, db = client
        _seed_reservation(db, "epic:x", 12)
        headers = _login(c)
        with patch("kew.labels.run") as gh:
            gh.return_value = MagicMock(returncode=0, stdout="", stderr="")
            r = c.post(
                "/api/verbs/epics/epic:x/retry/12",
                json={"confirm": True},
                headers=headers,
            )
        assert r.status_code == 200
        body = r.json()
        assert body["epic_ref"] == "epic:x"
        assert body["issue"] == 12
        assert body["item_id"] == "12"
        # The fixture has no run row for this reservation, so its generation is
        # indeterminate and retry must not release it.
        assert body["re_admitted"] is False
        # Indeterminate reservation preserved server-side.
        store = SqliteRunStore(db)
        assert [row.run_id for row in store.list_epic_reservations("epic:x")] == [
            "run-a"
        ]
        store.close()
        events = _audit_events(db, "epic_node_retry")
        assert len(events) == 1 and events[0].payload["node"] == "12"

    def test_retry_non_canonical_id_400(self, client):
        """A non-canonical id for the configured tracker is a 400, not a 422
        or a silent no-op — the route accepts both currencies but validates."""
        c, db = client
        headers = _login(c)
        r = c.post(
            "/api/verbs/epics/epic:x/retry/not-an-id",
            json={"confirm": True},
            headers=headers,
        )
        assert r.status_code == 400
        assert _audit_events(db, "epic_node_retry") == []

    def test_retry_held_node_400_and_hold_survives(self, client):
        """#2154: the web surface must refuse a held node too, not claim a retry."""
        c, db = client
        store = SqliteRunStore(db)
        store.hold_epic_node_item("epic:x", "12", "review_unparseable")
        store.close()
        headers = _login(c)
        with patch("kew.labels.run") as gh:
            gh.return_value = MagicMock(returncode=0, stdout="", stderr="")
            r = c.post(
                "/api/verbs/epics/epic:x/retry/12",
                json={"confirm": True},
                headers=headers,
            )
        assert r.status_code == 400
        assert "review_unparseable" in r.json()["detail"]
        gh.assert_not_called()
        store = SqliteRunStore(db)
        assert [row.item_id for row in store.list_epic_holds("epic:x")] == ["12"]
        store.close()
        assert _audit_events(db, "epic_node_retry") == []


class TestAckVerb:
    def test_ack_sets_acked_at_returns_state_and_emits(self, client):
        c, db = client
        _seed_orphan(db, 7)
        headers = _login(c)
        r = c.post("/api/verbs/orphans/7/ack", headers=headers)
        assert r.status_code == 200
        body = r.json()
        assert body["pr_number"] == 7
        assert body["acked"] is True
        assert body["acked_at"] is not None
        events = _audit_events(db, "orphan_pr_acked")
        assert len(events) == 1 and events[0].payload["pr_number"] == 7

    def test_ack_unknown_pr_returns_false_state(self, client):
        c, _db = client
        headers = _login(c)
        r = c.post("/api/verbs/orphans/999/ack", headers=headers)
        assert r.status_code == 200
        assert r.json()["acked"] is False


# ── --expose never loosens write-gating ─────────────────────────────────


class TestExposeSemantics:
    def test_write_still_requires_session_under_expose(self, tmp_path, inbox_client):
        app, _db = _make_app(tmp_path, expose=True)
        c = inbox_client(app, loopback_peer=True)
        # Even from a loopback peer, a write with no session is 401 under expose.
        assert c.post("/api/verbs/orphans/7/ack").status_code == 401


# ---------------------------------------------------------------------------
# I11 (Task 48): the no-change verbs are reachable ONLY through the audited
# service verbs, behind the same write gate as every other mutation.
#
# The routes add no judgment of their own — they translate the service layer's
# refusals into status codes. What is asserted here is that they cannot be
# reached without a session and CSRF, that an unauthorized actor gets 403
# rather than a quiet no-op, and that a successful confirm leaves the chained
# audit event the outcome's only readable account.
# ---------------------------------------------------------------------------

NC_BASE = "e" * 40
NC_PKG = "sha256:" + "f" * 64
NC_CHECK = "sha256:" + "a" * 64
NC_NOW = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)


def _seed_no_change(db: str, item: str = "1533") -> str:
    """One attested candidate waiting for a person (the I10B resting state)."""
    from kew.no_change import NamedCheck, NoChangeEvidence, NoChangeIntent

    store = SqliteRunStore(db)
    try:
        judgment = store.open_no_change_judgment(
            intent=NoChangeIntent(
                source_kind="github",
                source_id=item,
                run_id="run-" + item,
                attempt_number=1,
                base_sha=NC_BASE,
                item_id=item,
            ),
            now=NC_NOW,
        )
        store.attest_no_change(
            judgment_id=judgment.judgment_id,
            evidence=NoChangeEvidence(
                trusted_base_sha=NC_BASE,
                observed_base_sha=NC_BASE,
                package_digest=NC_PKG,
                package_scope="workspace",
                package_entry_count=0,
                package_deletion_count=0,
                checks=(NamedCheck(name="pytest", outcome="passed", digest=NC_CHECK),),
            ),
            now=NC_NOW,
        )
        store.decide_no_change(judgment_id=judgment.judgment_id, now=NC_NOW)
        return str(judgment.judgment_id)
    finally:
        store.close()


class _NoChangeTracker:
    def __init__(self):
        self.writes = []

    def observe(self, *, marker):
        from kew.no_change import ITEM_OPEN, ItemObservation

        return ItemObservation(available=True, state=ITEM_OPEN, marker_found=False)

    def apply(self, *, body, close):
        self.writes.append({"body": body, "close": close})
        return True


@contextmanager
def _no_change_remote(tracker=None):
    """The forge, answered: the base has not moved and the tracker says yes."""
    with (
        patch("kew.outcomes.run") as gh,
        patch(
            "kew.outcomes.no_change_item_adapter",
            return_value=tracker if tracker is not None else _NoChangeTracker(),
        ),
    ):
        gh.return_value = MagicMock(returncode=0, stdout=NC_BASE, stderr="")
        yield gh


def _no_change_app(tmp_path, approvers=("ada",)):
    app, db = _make_app(tmp_path)
    app.state.config.human_review.approvers = list(approvers)
    return app, db


class TestNoChangeAuthMatrix:
    def test_no_change_confirm_unauth_401(self, client):
        c, _db = client
        assert c.post("/api/verbs/no-change/nc-1/confirm").status_code == 401

    def test_no_change_confirm_session_no_csrf_403(self, client):
        c, _db = client
        c.post("/api/auth/login", json={"token": "TESTTOKEN"})
        assert c.post("/api/verbs/no-change/nc-1/confirm").status_code == 403

    def test_no_change_reject_unauth_401(self, client):
        c, _db = client
        r = c.post("/api/verbs/no-change/nc-1/reject", json={"reason": "no"})
        assert r.status_code == 401

    def test_no_change_retry_unauth_401(self, client):
        c, _db = client
        assert c.post("/api/verbs/no-change/nc-1/retry-sync").status_code == 401

    def test_no_change_retry_session_no_csrf_403(self, client):
        c, _db = client
        c.post("/api/auth/login", json={"token": "TESTTOKEN"})
        assert c.post("/api/verbs/no-change/nc-1/retry-sync").status_code == 403


class TestNoChangeVerbRoutes:
    def test_no_change_confirm_synchronizes_and_audits(
        self, tmp_path, inbox_client, monkeypatch
    ):
        monkeypatch.setenv("KEW_ACTOR", "ada")
        app, db = _no_change_app(tmp_path)
        judgment_id = _seed_no_change(db)
        c = inbox_client(app, loopback_peer=True)
        headers = _login(c)
        tracker = _NoChangeTracker()
        with _no_change_remote(tracker):
            r = c.post(f"/api/verbs/no-change/{judgment_id}/confirm", headers=headers)
        assert r.status_code == 200
        body = r.json()
        assert body["state"] == "completed"
        assert [job["state"] for job in body["sync"]] == ["acked"]
        assert tracker.writes and tracker.writes[0]["close"] is True
        events = _audit_events(db, "no_change_confirmed")
        assert len(events) == 1 and events[0].payload["source"] == "web"

    def test_no_change_confirm_denied_for_an_unnamed_actor_403(
        self, tmp_path, inbox_client, monkeypatch
    ):
        monkeypatch.setenv("KEW_ACTOR", "mallory")
        app, db = _no_change_app(tmp_path)
        judgment_id = _seed_no_change(db)
        c = inbox_client(app, loopback_peer=True)
        headers = _login(c)
        with _no_change_remote():
            r = c.post(f"/api/verbs/no-change/{judgment_id}/confirm", headers=headers)
        assert r.status_code == 403
        assert _audit_events(db, "no_change_confirmed") == []

    def test_no_change_confirm_unknown_judgment_404(
        self, tmp_path, inbox_client, monkeypatch
    ):
        monkeypatch.setenv("KEW_ACTOR", "ada")
        app, _db = _no_change_app(tmp_path)
        c = inbox_client(app, loopback_peer=True)
        headers = _login(c)
        with _no_change_remote():
            r = c.post("/api/verbs/no-change/nc-missing/confirm", headers=headers)
        assert r.status_code == 404

    def test_no_change_confirm_settled_judgment_409(
        self, tmp_path, inbox_client, monkeypatch
    ):
        monkeypatch.setenv("KEW_ACTOR", "ada")
        app, db = _no_change_app(tmp_path)
        judgment_id = _seed_no_change(db)
        c = inbox_client(app, loopback_peer=True)
        headers = _login(c)
        with _no_change_remote():
            first = c.post(
                f"/api/verbs/no-change/{judgment_id}/confirm", headers=headers
            )
            second = c.post(
                f"/api/verbs/no-change/{judgment_id}/confirm", headers=headers
            )
        assert first.status_code == 200 and second.status_code == 409

    def test_no_change_confirm_stale_base_409(
        self, tmp_path, inbox_client, monkeypatch
    ):
        monkeypatch.setenv("KEW_ACTOR", "ada")
        app, db = _no_change_app(tmp_path)
        judgment_id = _seed_no_change(db)
        c = inbox_client(app, loopback_peer=True)
        headers = _login(c)
        with patch("kew.outcomes.run") as gh:
            gh.return_value = MagicMock(returncode=0, stdout="9" * 40, stderr="")
            r = c.post(f"/api/verbs/no-change/{judgment_id}/confirm", headers=headers)
        assert r.status_code == 409
        assert _audit_events(db, "no_change_confirmed") == []

    def test_no_change_reject_without_a_reason_400(
        self, tmp_path, inbox_client, monkeypatch
    ):
        monkeypatch.setenv("KEW_ACTOR", "ada")
        app, db = _no_change_app(tmp_path)
        judgment_id = _seed_no_change(db)
        c = inbox_client(app, loopback_peer=True)
        headers = _login(c)
        with _no_change_remote():
            r = c.post(
                f"/api/verbs/no-change/{judgment_id}/reject",
                json={"reason": "  "},
                headers=headers,
            )
        assert r.status_code == 400

    def test_no_change_reject_leaves_the_item_open(
        self, tmp_path, inbox_client, monkeypatch
    ):
        monkeypatch.setenv("KEW_ACTOR", "ada")
        app, db = _no_change_app(tmp_path)
        judgment_id = _seed_no_change(db)
        c = inbox_client(app, loopback_peer=True)
        headers = _login(c)
        tracker = _NoChangeTracker()
        with _no_change_remote(tracker):
            r = c.post(
                f"/api/verbs/no-change/{judgment_id}/reject",
                json={"reason": "the checks never ran"},
                headers=headers,
            )
        assert r.status_code == 200 and r.json()["state"] == "rejected"
        assert tracker.writes[0]["close"] is False
        assert len(_audit_events(db, "no_change_rejected")) == 1

    def test_no_change_retry_sync_is_audited(self, tmp_path, inbox_client, monkeypatch):
        monkeypatch.setenv("KEW_ACTOR", "ada")
        app, db = _no_change_app(tmp_path)
        judgment_id = _seed_no_change(db)
        c = inbox_client(app, loopback_peer=True)
        headers = _login(c)
        with (
            patch("kew.outcomes.run") as gh,
            patch("kew.outcomes.no_change_item_adapter", return_value=None),
        ):
            gh.return_value = MagicMock(returncode=0, stdout=NC_BASE, stderr="")
            confirmed = c.post(
                f"/api/verbs/no-change/{judgment_id}/confirm", headers=headers
            )
        assert confirmed.json()["state"] == "sync_pending"
        tracker = _NoChangeTracker()
        with _no_change_remote(tracker):
            r = c.post(
                f"/api/verbs/no-change/{judgment_id}/retry-sync", headers=headers
            )
        assert r.status_code == 200
        assert [job["state"] for job in r.json()["sync"]] == ["acked"]
        assert len(_audit_events(db, "no_change_sync_retried")) == 1


# ── I20: the one guarded work action ────────────────────────────────────
#
# The route holds no action vocabulary of its own. A client posts an action the
# projection offered and the revision it read it at; the server resolves the
# target, the authority and whether the action may be requested at all. So the
# interesting tests here are the refusals — each one is a request a client
# could construct but must not be able to complete.

WA_NOW = datetime(2026, 8, 22, 9, 0, tzinfo=UTC)


def _seed_preserved(
    db: str, item_id: str = "42", run_id: str = "run-preserved"
) -> None:
    store = SqliteRunStore(db)
    store.admit_run_with_identity(
        RunRecord(
            run_id=run_id,
            issue_num=int(item_id) if item_id.isdigit() else 0,
            item_id=item_id,
            branch=f"agent/issue-{item_id.lower()}",
            status="awaiting_publication",
            exit_code=0,
            started_at=WA_NOW,
            finished_at=WA_NOW,
            attempt_number=1,
        ),
        AdmissionIdentity(
            run_id=run_id,
            attempt_number=1,
            source_tracker="github" if item_id.isdigit() else "linear",
            item_id=item_id,
            delivery_repo="o/r",
        ),
    )
    store.close()


def _seed_pending_no_change(db: str, item: str = "42") -> None:
    """One zero-diff judgment waiting for a person, as I10B leaves it."""
    from kew.no_change import NamedCheck, NoChangeEvidence, NoChangeIntent

    store = SqliteRunStore(db)
    judgment = store.open_no_change_judgment(
        intent=NoChangeIntent(
            source_kind="github",
            source_id=item,
            run_id=f"run-{item}",
            attempt_number=1,
            base_sha="a" * 40,
            item_id=item,
        ),
        now=WA_NOW,
    )
    store.attest_no_change(
        judgment_id=judgment.judgment_id,
        evidence=NoChangeEvidence(
            trusted_base_sha="a" * 40,
            observed_base_sha="a" * 40,
            package_digest="sha256:" + "c" * 64,
            package_scope="workspace",
            package_entry_count=0,
            package_deletion_count=0,
            checks=(
                NamedCheck(
                    name="pytest", outcome="passed", digest="sha256:" + "d" * 64
                ),
            ),
        ),
        now=WA_NOW,
    )
    store.decide_no_change(judgment_id=judgment.judgment_id, now=WA_NOW)
    store.close()


def _work_view(c, item_id: str = "42") -> dict[str, Any]:
    """The view a client decides against — read through the public surface."""
    r = c.get(f"/api/verbs/work/{item_id}")
    assert r.status_code == 200, r.text
    return r.json()


def _act(c, headers, item_id: str, action: str, revision: str, **extra):
    return c.post(
        f"/api/verbs/work/{item_id}/action",
        json={"action": action, "expected_revision": revision, **extra},
        headers=headers,
    )


class TestWorkActionAuthMatrix:
    def test_work_action_unauth_401(self, client):
        c, db = client
        _seed_preserved(db)
        r = c.post(
            "/api/verbs/work/42/action",
            json={"action": "finalize_publication", "expected_revision": "wv1:x"},
        )
        assert r.status_code == 401

    def test_work_action_session_no_csrf_403(self, client):
        c, db = client
        _seed_preserved(db)
        c.post("/api/auth/login", json={"token": "TESTTOKEN"})
        r = c.post(
            "/api/verbs/work/42/action",
            json={"action": "finalize_publication", "expected_revision": "wv1:x"},
        )
        assert r.status_code == 403

    def test_work_action_non_approver_403(self, tmp_path, inbox_client, monkeypatch):
        monkeypatch.setenv("KEW_ACTOR", "mallory")
        app, db = _make_app(tmp_path, approvers=["alice"])
        c = inbox_client(app, loopback_peer=True)
        _seed_preserved(db)
        headers = _login(c)
        view = _work_view(c)

        r = _act(c, headers, "42", "finalize_publication", view["view_revision"])

        assert r.status_code == 403
        assert _audit_events(db, "work_action_requested") == []

    def test_work_view_read_needs_a_session_when_exposed(self, tmp_path, inbox_client):
        """The action surface's own read is a read: under ``--expose`` it is
        session-gated like every other one (F7)."""
        app, db = _make_app(tmp_path, expose=True)
        # tls=True: exposed mode sets Secure cookies, which a cookie jar only
        # replays over https — the browser's side of the TLS proxy (W1.6).
        c = inbox_client(app, loopback_peer=True, tls=True)
        _seed_preserved(db)

        assert c.get("/api/verbs/work/42").status_code == 401

        _login(c)
        assert c.get("/api/verbs/work/42").status_code == 200


class TestWorkActionContract:
    def test_the_offered_action_applies_and_returns_the_post_write_view(
        self, tmp_path, inbox_client, monkeypatch
    ):
        monkeypatch.setenv("KEW_ACTOR", "alice")
        app, db = _make_app(tmp_path, approvers=["alice"])
        c = inbox_client(app, loopback_peer=True)
        _seed_preserved(db)
        headers = _login(c)
        view = _work_view(c)
        assert [a["action"] for a in view["actions"]] == ["finalize_publication"]

        r = _act(c, headers, "42", "finalize_publication", view["view_revision"])

        assert r.status_code == 200
        body = r.json()
        assert body["applied"] is True and body["idempotent"] is False
        # The client never sent a run id; the server read it from the facts.
        assert (body["target_kind"], body["target_ref"]) == ("run", "run-preserved")
        assert body["view"]["view_revision"] != view["view_revision"]
        assert body["view"]["artifact"]["publication"] != "pending"
        assert len(_audit_events(db, "work_action_applied")) == 1

    def test_a_replayed_request_returns_the_same_truth_without_acting_twice(
        self, tmp_path, inbox_client, monkeypatch
    ):
        monkeypatch.setenv("KEW_ACTOR", "alice")
        app, db = _make_app(tmp_path, approvers=["alice"])
        c = inbox_client(app, loopback_peer=True)
        _seed_preserved(db)
        headers = _login(c)
        revision = _work_view(c)["view_revision"]
        first = _act(c, headers, "42", "finalize_publication", revision)

        second = _act(c, headers, "42", "finalize_publication", revision)

        assert (first.status_code, second.status_code) == (200, 200)
        assert second.json()["idempotent"] is True
        assert second.json()["applied"] is False
        assert len(_audit_events(db, "publication_finalized")) == 1

    def test_a_stale_revision_is_409_and_changes_nothing(
        self, tmp_path, inbox_client, monkeypatch
    ):
        monkeypatch.setenv("KEW_ACTOR", "alice")
        app, db = _make_app(tmp_path, approvers=["alice"])
        c = inbox_client(app, loopback_peer=True)
        _seed_preserved(db)
        headers = _login(c)

        r = _act(c, headers, "42", "finalize_publication", "wv1:0000000000000000")

        assert r.status_code == 409
        assert "moved" in r.json()["detail"]
        store = SqliteRunStore(db)
        assert store.get("run-preserved").status == "awaiting_publication"
        store.close()
        assert _audit_events(db, "work_action_requested") == []

    def test_no_client_can_ask_for_a_redispatch_over_preserved_work(
        self, tmp_path, inbox_client, monkeypatch
    ):
        """The guardrail at the network edge: the action is not merely absent
        from the offered list, it is refused when asked for directly."""
        monkeypatch.setenv("KEW_ACTOR", "alice")
        app, db = _make_app(tmp_path, approvers=["alice"])
        c = inbox_client(app, loopback_peer=True)
        _seed_preserved(db)
        headers = _login(c)
        revision = _work_view(c)["view_revision"]

        r = _act(c, headers, "42", "dispatch", revision)

        assert r.status_code == 409
        assert "not offered" in r.json()["detail"]
        store = SqliteRunStore(db)
        assert store.get("run-preserved").status == "awaiting_publication"
        store.close()
        assert _audit_events(db, "issue_queued") == []

    def test_an_opaque_item_id_rides_the_path_unchanged(
        self, tmp_path, inbox_client, monkeypatch
    ):
        monkeypatch.setenv("KEW_ACTOR", "alice")
        app, db = _make_app(tmp_path, approvers=["alice"])
        c = inbox_client(app, loopback_peer=True)
        _seed_preserved(db, item_id="SFD-12", run_id="run-sfd")
        headers = _login(c)
        view = _work_view(c, "SFD-12")
        assert view["identity"]["item_ref"] == "SFD-12"

        r = _act(c, headers, "SFD-12", "finalize_publication", view["view_revision"])

        assert r.status_code == 200
        assert r.json()["target_ref"] == "run-sfd"
        assert r.json()["view"]["identity"]["item_id"] == "SFD-12"

    def test_a_decision_that_must_be_explained_is_400_without_a_reason(
        self, tmp_path, inbox_client, monkeypatch
    ):
        """Refusing a zero-diff outcome is the one offer that owes a reason,
        and the projection says so before the request is ever made."""
        monkeypatch.setenv("KEW_ACTOR", "alice")
        app, db = _make_app(tmp_path, approvers=["alice"])
        c = inbox_client(app, loopback_peer=True)
        _seed_pending_no_change(db)
        headers = _login(c)
        view = _work_view(c)
        assert [(a["action"], a["requires_reason"]) for a in view["actions"]] == [
            ("confirm_no_change", False),
            ("reject_no_change", True),
        ]

        r = _act(c, headers, "42", "reject_no_change", view["view_revision"])

        assert r.status_code == 400
        assert "requires a reason" in r.json()["detail"]
        assert _audit_events(db, "work_action_requested") == []

    def test_the_machine_contract_is_published_in_the_openapi_schema(
        self, tmp_path, inbox_client
    ):
        app, _db = _make_app(tmp_path)
        schema = app.openapi()
        route = schema["paths"]["/api/verbs/work/{item_id}/action"]["post"]
        body = route["requestBody"]["content"]["application/json"]["schema"]

        assert body["$ref"].endswith("WorkActionBody")
        required = schema["components"]["schemas"]["WorkActionBody"]["required"]
        assert set(required) == {"action", "expected_revision"}
        response = route["responses"]["200"]["content"]["application/json"]["schema"]
        assert response["$ref"].endswith("WorkActionResultDTO")
        result = schema["components"]["schemas"]["WorkActionResultDTO"]["properties"]
        assert result["view"]["$ref"].endswith("WorkViewDTO")
        actions = schema["components"]["schemas"]["WorkViewDTO"]["properties"][
            "actions"
        ]
        assert actions["items"]["$ref"].endswith("WorkActionDTO")


class TestWorkViewInFlightBound:
    """The single-item read composes under THIS host's in-flight bound.

    ``view_revision`` digests the composed view, and ``pending_run_max_age_s``
    (#2279/D8) is part of what composes it: a pending review round is a round
    kew is still reading until twice the operator's ``agent.timeout_minutes``,
    floored at four hours. The aggregate projection, the DAG's needs-you join
    and ``perform_work_action`` all pass that bound, so a single-item read that
    kept the bare floor published a different exception from the diamonds
    beside it — and a revision the action gate could never match, which refuses
    every action on the item for as long as the row stays pending.
    """

    @staticmethod
    def _seed_review_round(db: str, *, hours_ago: float, item_id: str = "42") -> None:
        """An open PR plus a review round that started *hours_ago*.

        Ages are taken against the wall clock these routes compose at, so the
        row is seeded relative to ``now`` rather than to a fixed instant.
        """
        from datetime import timedelta

        from kew.run_store import ADVERSARIAL_REVIEW_RUN_KIND

        now = datetime.now(UTC)
        store = SqliteRunStore(db)
        for run_id, fields in (
            (
                "impl-42",
                {
                    "status": "awaiting_publication",
                    "started_at": now - timedelta(hours=hours_ago + 2),
                    "finished_at": now - timedelta(hours=hours_ago + 1),
                    "pr_url": "https://example.invalid/pr/42",
                    "pr_state": "OPEN",
                    "attempt_number": 1,
                },
            ),
            (
                "rev-42",
                {
                    "status": "pending",
                    "started_at": now - timedelta(hours=hours_ago),
                    "finished_at": None,
                    "run_kind": ADVERSARIAL_REVIEW_RUN_KIND,
                    "attempt_number": 2,
                },
            ),
        ):
            store.admit_run_with_identity(
                RunRecord(
                    run_id=run_id,
                    issue_num=int(item_id),
                    item_id=item_id,
                    branch=f"agent/issue-{item_id}",
                    exit_code=0,
                    **fields,
                ),
                AdmissionIdentity(
                    run_id=run_id,
                    attempt_number=int(fields["attempt_number"]),
                    source_tracker="github",
                    item_id=item_id,
                    delivery_repo="o/r",
                ),
            )
        store.close()

    def _projected(self, c, item_id: str = "42") -> dict[str, Any]:
        r = c.get("/api/dashboard/work")
        assert r.status_code == 200, r.text
        return next(v for v in r.json()["views"] if v["identity"]["item_id"] == item_id)

    def test_a_raised_agent_budget_is_read_by_the_single_item_view_too(
        self, tmp_path, inbox_client
    ):
        """One host, one reading: a five-hour round on a six-hour agent is live.

        The drawer on the epic page reads this route while the diamonds beside
        it come from the projection. Composed against the four-hour floor, this
        route alone called the round abandoned and offered ``inspect``.
        """
        app, db = _make_app(tmp_path, timeout_minutes=360)
        c = inbox_client(app, loopback_peer=True)
        self._seed_review_round(db, hours_ago=5)

        view = _work_view(c)

        assert view["exception"]["state"] == "review_in_flight"
        assert view["safest_action"]["action"] == "wait"
        assert view["exception"]["state"] == self._projected(c)["exception"]["state"]

    def test_the_four_hour_floor_still_bounds_a_default_host(
        self, tmp_path, inbox_client
    ):
        """The bound is derived, not loosened: on a default budget the same
        five-hour row is the dead row it is, on both surfaces."""
        app, db = _make_app(tmp_path)
        c = inbox_client(app, loopback_peer=True)
        self._seed_review_round(db, hours_ago=5)

        view = _work_view(c)

        assert view["exception"]["state"] == "review_abandoned"
        assert view["exception"]["state"] == self._projected(c)["exception"]["state"]

    def test_the_published_revision_is_the_one_the_action_gate_recomposes(
        self, tmp_path, inbox_client, monkeypatch
    ):
        """The gate's "current view" IS this route's revision (#2279/D8).

        ``perform_work_action`` recomposes the view under
        ``pending_run_max_age_s(config)`` before checking the caller's
        ``expected_revision``, so a route composing under a different age made
        every action on a pending item permanently stale — the refusal names
        the current revision, which is how a client proves the two agree.
        """
        monkeypatch.setenv("KEW_ACTOR", "alice")
        app, db = _make_app(tmp_path, approvers=["alice"], timeout_minutes=360)
        c = inbox_client(app, loopback_peer=True)
        self._seed_review_round(db, hours_ago=5)
        headers = _login(c)
        view = _work_view(c)

        r = _act(c, headers, "42", "finalize_publication", "wv1:not-the-revision")

        assert r.status_code == 409
        detail = r.json()["detail"]
        assert "moved" in detail
        assert view["view_revision"] in detail
