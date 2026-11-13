"""Tests for the kew serve Loop read endpoints (read-only)."""

from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from kew.serve.app import create_app


class FakeLoop:
    def __init__(self, loop_id: str = "L1", status: str = "pursuing") -> None:
        self.loop_id = loop_id
        self.source = "issue"
        self.issue_num = 281
        self.repo = "thossullivan/kew"
        self.goal_text = "make CI green"
        self.runner_name = "claude-code"
        self.status = status
        self.status_reason = (
            "" if status != "needs_human" else "gate_failed: 2 tests red"
        )
        self.budget_usd = 5.0
        self.spent_usd = 1.25
        self.wall_clock_secs = 1800
        self.max_steps = 200
        self.step_count = 1
        self.created_at = datetime(2026, 6, 23, tzinfo=UTC)
        self.updated_at = None
        self.finished_at = None


class FakeIteration:
    def __init__(self, idx: int = 0, status: str = "pursuing") -> None:
        self.iteration_id = f"I{idx}"
        self.loop_id = "L1"
        self.idx = idx
        self.run_id = "run-abc"
        self.kind = "assistant_turn"
        self.status = status
        self.cost_usd = 0.42
        self.tokens = 900
        self.started_at = datetime(2026, 6, 23, 0, 1, tzinfo=UTC)
        self.finished_at = datetime(2026, 6, 23, 0, 2, tzinfo=UTC)


class FakeAudit:
    """Structural stand-in for run_store.AuditEvent (payload holds loop_id)."""

    def __init__(self, id: int, event: str, loop_id: str, summary: str = "") -> None:
        self.id = id
        self.timestamp = "2026-06-23T00:01:00+00:00"
        self.event = event
        self.actor_type = "policy"
        self.actor_id = None
        self.payload = {"loop_id": loop_id, "summary": summary}


class FakeStore:
    def __init__(self, loops: list[FakeLoop]) -> None:
        self._loops = loops

    # runs surface still required by create_app's run routes
    def list_all(self) -> list[object]:
        return []

    def list_loops(self, status: str | None = None) -> list[FakeLoop]:
        if status is None:
            return list(self._loops)
        return [loop for loop in self._loops if loop.status == status]

    def get_loop(self, loop_id: str) -> FakeLoop | None:
        return next((loop for loop in self._loops if loop.loop_id == loop_id), None)

    def list_iterations(self, loop_id: str) -> list[FakeIteration]:
        return [FakeIteration()] if loop_id == "L1" else []

    def list_iterations_tail(
        self, loop_id: str, *, limit: int = 200
    ) -> list[FakeIteration]:
        if loop_id != "L1":
            return []
        every = [FakeIteration(i, "failed" if i == 1 else "pursuing") for i in range(5)]
        return every[-limit:]

    def list_audit_events(
        self,
        limit: int | None = 100,
        *,
        event: str | None = None,
        since: object | None = None,
        loop_id: str | None = None,
    ) -> list[FakeAudit]:
        # Two L1 events (newest-first) + one for another loop that must be filtered out.
        return [
            FakeAudit(3, "verification_checked", "L1", "gate failed: 2 tests red"),
            FakeAudit(2, "loop_started", "L1"),
            FakeAudit(1, "loop_started", "L-other"),
        ]


@pytest.fixture
def client(tmp_path: Path, inbox_client) -> TestClient:
    store = FakeStore([FakeLoop("L1", "pursuing"), FakeLoop("L2", "needs_human")])
    app = create_app(lambda: nullcontext(store), repo_dir=tmp_path)
    return inbox_client(app, loopback_peer=True)


def test_list_loops(client: TestClient):
    r = client.get("/api/loops")
    assert r.status_code == 200
    body = r.json()
    assert [loop["loop_id"] for loop in body] == ["L1", "L2"]
    assert body[0]["drive_mode"] == "delegated"
    assert body[0]["created_at"] == "2026-06-23T00:00:00+00:00"


def test_list_loops_status_filter(client: TestClient):
    body = client.get("/api/loops?status=needs_human").json()
    assert [loop["loop_id"] for loop in body] == ["L2"]
    assert body[0]["needs_human"] is True


def test_get_loop_detail_includes_iterations(client: TestClient):
    r = client.get("/api/loops/L1")
    assert r.status_code == 200
    body = r.json()
    assert body["loop_id"] == "L1"
    assert body["goal_text"] == "make CI green"
    assert len(body["iterations"]) == 1
    assert body["iterations"][0]["kind"] == "assistant_turn"
    assert body["iterations"][0]["cost_usd"] == 0.42


def test_get_loop_unknown_404(client: TestClient):
    assert client.get("/api/loops/nope").status_code == 404


def test_loop_iterations_is_its_own_bounded_read(client: TestClient):
    """#2304 — the iteration strip reads a bounded page, not the whole loop.

    `/loops/{id}` returns every iteration a loop ever ran, so the roster's
    strip could not read it per row. This route is tail-bounded in SQL.
    """
    r = client.get("/api/loops/L1/iterations")
    assert r.status_code == 200
    body = r.json()
    assert [it["idx"] for it in body] == [0, 1, 2, 3, 4]
    assert body[1]["status"] == "failed"

    tail = client.get("/api/loops/L1/iterations?limit=2").json()
    assert [it["idx"] for it in tail] == [3, 4]

    assert client.get("/api/loops/L1/iterations?limit=0").status_code == 422
    assert client.get("/api/loops/L1/iterations?limit=10000").status_code == 422
    # A loop with no iterations is 200 and empty, exactly like /audit.
    assert client.get("/api/loops/L-none/iterations").json() == []


def test_get_loop_audit_filters_by_loop_id(client: TestClient):
    r = client.get("/api/loops/L1/audit")
    assert r.status_code == 200
    body = r.json()
    # Only the two L1 events, in store order (newest-first); L-other filtered out.
    assert [e["event"] for e in body] == ["verification_checked", "loop_started"]
    assert all(e["loop_id"] == "L1" for e in body)
    assert body[0]["summary"] == "gate failed: 2 tests red"


def test_get_loop_audit_empty_for_unknown_loop_is_200(client: TestClient):
    # A loop with no audit events is valid — empty list, not 404.
    r = client.get("/api/loops/L-none/audit")
    assert r.status_code == 200
    assert r.json() == []


def test_get_loop_detail_status_reason_passthrough(client: TestClient):
    body = client.get("/api/loops/L2").json()
    assert body["status"] == "needs_human"
    assert body["needs_human"] is True
    assert body["status_reason"] == "gate_failed: 2 tests red"


def test_get_loop_detail_budget_line_fields_present(client: TestClient):
    # The read card renders spent_usd vs budget_usd; both must serialize.
    body = client.get("/api/loops/L1").json()
    assert body["spent_usd"] == 1.25
    assert body["budget_usd"] == 5.0
    assert body["max_steps"] == 200


def test_loops_endpoint_is_read_only(client: TestClient):
    # No mutation verbs exist on the loop routes (read-only v1). The paths DO
    # have GET routes, so a mutating verb on them is 405 Method Not Allowed
    # (not 404 — that would only apply to an undefined path).
    assert client.post("/api/loops").status_code == 405
    assert client.delete("/api/loops/L1").status_code == 405
