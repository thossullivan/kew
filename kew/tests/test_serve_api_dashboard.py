"""Serve-surface coverage for GET /api/dashboard (#700, Group 1).

The aggregate read sits behind ``read_guard`` on an ``/api/*`` path: session-
gated under ``--expose`` and for non-loopback peers, loopback-open when not
exposed (mirrors test_serve_api_authz). Also asserts the payload shape the
frontend groups consume.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import nullcontext
from pathlib import Path
from typing import Any

import pytest

from kew.config import DispatchConfig, ProjectConfig
from kew.serve.app import create_app
from kew.service import dashboard

PATH = "/api/dashboard"


class FakeStore:
    """Empty store: every reader ``assemble`` touches returns nothing."""

    def list_audit_events(
        self, *, limit=None, event_prefix=None, event=None, issue_nums=None
    ):
        return []

    def list_all_epic_holds(self):
        return []

    def list_orphan_prs(self, include_acked=False):
        return []

    def list_syncable_runs(self, *, item_ids=None):
        return []

    # The canonical work projection (I19) composes over these.
    def read_snapshot(self):
        return nullcontext()

    def list_by_item(self, item_id):
        return []

    def get_admission_identity(self, run_id):
        return None

    def list_all(self):
        return []

    def list_epic_refs(self):
        return []

    def get_epic_driver_state(self, epic_ref):
        return None

    def get_fleet_stopped(self):
        return None

    def list_pending(self):
        return []

    def list_since(self, since):
        return []

    def list_interactive_usage_since(self, since):
        return []


def _mk_app(tmp_path: Path, *, expose: bool):
    store = FakeStore()
    # repo="" keeps the ready-queue reader from shelling out to gh in tests.
    return create_app(
        lambda: nullcontext(store),
        repo_dir=tmp_path,
        expose=expose,
        config=DispatchConfig(),
    )


def test_exposed_read_requires_session(tmp_path: Path, inbox_client):
    """Under --expose, an unauthenticated read must 401 — even from loopback."""
    c = inbox_client(_mk_app(tmp_path, expose=True), loopback_peer=True)
    assert c.get(PATH).status_code == 401


def test_non_loopback_read_requires_session(tmp_path: Path, inbox_client):
    """Not exposed, but a non-loopback peer: read must 401 without a session."""
    c = inbox_client(_mk_app(tmp_path, expose=False), loopback_peer=False)
    assert c.get(PATH).status_code == 401


def test_loopback_read_open_when_not_exposed(tmp_path: Path, inbox_client):
    """The local single-operator flow reads without a session."""
    c = inbox_client(_mk_app(tmp_path, expose=False), loopback_peer=True)
    assert c.get(PATH).status_code == 200


def test_payload_shape(tmp_path: Path, inbox_client):
    c = inbox_client(_mk_app(tmp_path, expose=False), loopback_peer=True)
    body = c.get(PATH).json()
    assert set(body) >= {"banner", "rows", "glance", "as_of", "unavailable"}
    assert set(body["banner"]) == {"fleet_stopped", "no_live_driver"}
    assert body["rows"] == []
    assert body["unavailable"] == []
    assert set(body["glance"]) >= {
        "fleet",
        "caps",
        "routing",
        "ready_queue",
        "recent_completions",
    }


WORK_PATH = "/api/dashboard/work"


def test_work_projection_read_requires_a_session_when_exposed(
    tmp_path: Path, inbox_client
):
    """The new read inherits the router-level guard — no unguarded routes."""
    c = inbox_client(_mk_app(tmp_path, expose=True), loopback_peer=True)
    assert c.get(WORK_PATH).status_code == 401


def test_work_projection_non_loopback_read_requires_a_session(
    tmp_path: Path, inbox_client
):
    c = inbox_client(_mk_app(tmp_path, expose=False), loopback_peer=False)
    assert c.get(WORK_PATH).status_code == 401


def test_work_projection_payload_shape(tmp_path: Path, inbox_client):
    c = inbox_client(_mk_app(tmp_path, expose=False), loopback_peer=True)
    body = c.get(WORK_PATH).json()
    assert set(body) >= {
        "views",
        "pressure",
        "projection_revision",
        "composed_at",
        "unreadable",
    }
    assert body["views"] == []
    assert body["unreadable"] == []
    assert body["projection_revision"].startswith("wp1:")


def test_work_projection_reports_prevention_as_configured_but_inactive(
    tmp_path: Path, inbox_client
):
    """Pre-broker, the web surface must not claim the policy prevents anything."""
    c = inbox_client(_mk_app(tmp_path, expose=False), loopback_peer=True)
    pressure = c.get(WORK_PATH).json()["pressure"]
    assert pressure["enforcement"] == "inactive"
    assert "not active" in pressure["enforcement_detail"]


def test_dashboard_carries_the_same_pressure_the_projection_does(
    tmp_path: Path, inbox_client
):
    c = inbox_client(_mk_app(tmp_path, expose=False), loopback_peer=True)
    assert (
        c.get(PATH).json()["glance"]["pressure"] == c.get(WORK_PATH).json()["pressure"]
    )


# --------------------------------------------------------------------------- #
# The gh reads behind the dashboard stay off the request path (#2789, K8)
# --------------------------------------------------------------------------- #

REPO = "o/r"


class FakeGh:
    """``gh issue list`` at the process boundary. Each answer names the
    ``version`` it was read at; ``gate`` holds every read after warm-up until
    the test opens it, and ``finished`` counts the held reads that returned."""

    def __init__(self) -> None:
        self.version = 1
        self.warm = False
        self.gate = threading.Event()
        self.started = threading.Event()
        self.finished = 0
        self.fail = False

    def __call__(self, argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        if argv[:3] != ["gh", "issue", "list"]:
            return subprocess.CompletedProcess(argv, 1, "", "not gh issue list")
        version = self.version
        if self.warm:
            self.started.set()
            self.gate.wait(10)
            self.finished += 1
        if self.fail:
            return subprocess.CompletedProcess(argv, 1, "", "rate limited")
        labels = [{"name": "ready"}] if "--label" in argv else []
        issues = [{"number": version, "title": f"v{version}", "labels": labels}]
        return subprocess.CompletedProcess(argv, 0, json.dumps(issues), "")


@pytest.fixture
def gh(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeGh]:
    fake = FakeGh()
    monkeypatch.setattr(dashboard, "run", fake)
    for cache in (dashboard._ready_cache, dashboard._candidates_cache):
        cache.clear()
    dashboard._read_failures.clear()
    yield fake
    fake.gate.set()  # never leave a refresh thread parked on the gate


def _repo_app(tmp_path: Path):
    return create_app(
        lambda: nullcontext(FakeStore()),
        repo_dir=tmp_path,
        expose=False,
        config=DispatchConfig(project=ProjectConfig(repo=REPO)),
    )


def _expire_caches() -> None:
    """Age both gh caches past their TTL without touching the clock."""
    for cache in (dashboard._ready_cache, dashboard._candidates_cache):
        for repo, (_, value) in list(cache.items()):
            cache[repo] = (time.monotonic() - 3600.0, value)


def _wait_for(condition: Callable[[], bool]) -> None:
    deadline = time.monotonic() + 5.0
    while not condition():
        assert time.monotonic() < deadline, "the background refresh never landed"
        time.sleep(0.01)


def _numbers(body: dict[str, Any]) -> tuple[list[int], list[int]]:
    glance = body["glance"]
    return (
        [i["issue_num"] for i in glance["ready_queue"]],
        [i["issue_num"] for i in glance["queue_candidates"]],
    )


def test_dashboard_answers_from_a_stale_cache_while_the_refresh_runs(
    tmp_path: Path, inbox_client, gh: FakeGh
):
    c = inbox_client(_repo_app(tmp_path), loopback_peer=True)
    # Warm-up: the first read of each has nothing to serve, so it reads gh.
    assert _numbers(c.get(PATH).json()) == ([1], [1])

    _expire_caches()
    gh.version, gh.warm = 2, True
    body = c.get(PATH).json()

    # The answer came back while both refreshes were still parked in gh: had
    # either run on the request path, it would have returned before this did.
    assert gh.started.is_set()
    assert gh.finished == 0
    assert _numbers(body) == ([1], [1])
    assert "ready_queue" not in body["unavailable"]

    gh.gate.set()
    _wait_for(lambda: dashboard._ready_cache[REPO][1][0].issue_num == 2)
    _wait_for(lambda: gh.finished == 2)
    assert _numbers(c.get(PATH).json()) == ([2], [2])


def test_a_failed_background_refresh_reads_unavailable_not_stale(
    tmp_path: Path, inbox_client, gh: FakeGh
):
    c = inbox_client(_repo_app(tmp_path), loopback_peer=True)
    assert _numbers(c.get(PATH).json()) == ([1], [1])

    _expire_caches()
    gh.warm, gh.fail = True, True
    gh.gate.set()
    c.get(PATH)  # serves the stale value and starts the refresh
    _wait_for(lambda: gh.finished == 2)
    _wait_for(lambda: len(dashboard._read_failures) == 2)

    body = c.get(PATH).json()
    assert {"ready_queue", "queue_candidates"} <= set(body["unavailable"])
    assert gh.finished == 2  # held for the backoff: gh is not asked again
