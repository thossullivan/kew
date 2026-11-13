"""The console's Run button (#2537): ``/api/run-requests``.

The write is ``require_write`` (session + CSRF + Origin) like every other; it
queues a row and an audit fact and launches nothing. With no drive running it
says so instead of queueing into nothing.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from kew import run_requests as rr
from kew.config import load_config
from kew.drive import DriveLease, acquire_drive
from kew.drive_heartbeat import DRIVE_HEARTBEAT_KEY, DriveHeartbeat
from kew.run_store import SqliteRunStore
from kew.serve.app import create_app
from kew.serve.auth import SessionStore
from kew.service.run_requests import LIST_LIMIT, LIST_WINDOW

UNHOLD = "kew epic unhold epic:a 12"


@contextmanager
def _store_factory(db_path: str) -> Any:
    store = SqliteRunStore(db_path)
    try:
        yield store
    finally:
        store.close()


@pytest.fixture
def client(tmp_path: Path, inbox_client: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("KEW_ACTOR", "tom")
    db = str(tmp_path / "kew.db")
    SqliteRunStore(db).close()
    cfg = load_config().model_copy(deep=True)
    cfg.project.repo = "o/r"
    app = create_app(
        lambda: _store_factory(db),
        repo_dir=tmp_path,
        config=cfg,
        token="TESTTOKEN",
        sessions=SessionStore(),
        bind_host="127.0.0.1",
        bind_port=8765,
    )
    return inbox_client(app, loopback_peer=True), db


def _login(c: Any) -> dict[str, str]:
    r = c.post("/api/auth/login", json={"token": "TESTTOKEN"})
    assert r.status_code == 200
    return {"X-Kew-CSRF": r.json()["csrf_token"]}


def _live_drive(db: str) -> None:
    with SqliteRunStore(db) as store:
        lease = acquire_drive(store)
        assert isinstance(lease, DriveLease)
        beat = DriveHeartbeat(
            token=lease.holder.token,
            pid=lease.holder.pid,
            host=lease.holder.host,
            at=datetime.now(UTC),
            every_s=30.0,
            stage="sleeping",
        )
        store.set_meta(DRIVE_HEARTBEAT_KEY, beat.encode())


def _requests(db: str) -> list[rr.RunRequest]:
    with SqliteRunStore(db) as store:
        return rr.list_requests(store)


def test_a_run_needs_a_session(client: Any) -> None:
    c, db = client
    _live_drive(db)
    assert c.post("/api/run-requests", json={"command": UNHOLD}).status_code == 401
    assert _requests(db) == []


def test_a_run_needs_the_csrf_header(client: Any) -> None:
    c, db = client
    _live_drive(db)
    _login(c)
    assert c.post("/api/run-requests", json={"command": UNHOLD}).status_code == 403
    assert _requests(db) == []


def test_a_run_queues_the_command_for_the_drive(client: Any) -> None:
    c, db = client
    _live_drive(db)
    r = c.post(
        "/api/run-requests",
        json={"command": UNHOLD, "item_id": "12"},
        headers=_login(c),
    )

    assert r.status_code == 200
    body = r.json()
    assert body["created"] is True
    assert body["request"]["state"] == "queued"
    assert body["request"]["operator"] == "tom"
    [request] = _requests(db)
    assert request.command == UNHOLD

    one = c.get(f"/api/run-requests/{request.id}")
    assert one.status_code == 200 and one.json()["id"] == request.id
    listed = c.get("/api/run-requests", params={"command": UNHOLD}).json()
    assert [x["id"] for x in listed["requests"]] == [request.id]
    assert listed["drive"]["running"] is True


def test_a_run_with_no_drive_says_no_drive_running(client: Any) -> None:
    c, db = client
    r = c.post("/api/run-requests", json={"command": UNHOLD}, headers=_login(c))

    assert r.status_code == 409
    assert "no drive running" in r.json()["detail"]
    assert _requests(db) == []
    assert c.get("/api/run-requests").json()["drive"]["running"] is False


def test_an_unrunnable_command_is_422_and_queues_nothing(client: Any) -> None:
    c, db = client
    _live_drive(db)
    r = c.post(
        "/api/run-requests",
        json={"command": "kew epic run epic:a"},
        headers=_login(c),
    )
    assert r.status_code == 422
    assert _requests(db) == []


def test_an_unknown_request_is_404(client: Any) -> None:
    c, _db = client
    assert c.get("/api/run-requests/nope").status_code == 404


def test_a_non_approver_run_is_403_and_queues_nothing(
    tmp_path: Path, inbox_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KEW_ACTOR", "mallory")
    db = str(tmp_path / "kew.db")
    SqliteRunStore(db).close()
    cfg = load_config().model_copy(deep=True)
    cfg.project.repo = "o/r"
    cfg.approval.approvers = ["tom"]
    app = create_app(
        lambda: _store_factory(db),
        repo_dir=tmp_path,
        config=cfg,
        token="TESTTOKEN",
        sessions=SessionStore(),
        bind_host="127.0.0.1",
        bind_port=8765,
    )
    c = inbox_client(app, loopback_peer=True)
    _live_drive(db)
    r = c.post("/api/run-requests", json={"command": UNHOLD}, headers=_login(c))
    assert r.status_code == 403
    assert _requests(db) == []


# --------------------------------------------------------------------------- #
# The launchpad's model (#2741)
# --------------------------------------------------------------------------- #

DISPATCH = "kew dispatch 42"


@pytest.fixture
def claude_route(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every tier routes to claude-code, whatever the checkout's kew.toml says."""
    from kew.config import DispatchConfig, RouteEntry, RoutingConfig

    cfg = DispatchConfig(
        routing=RoutingConfig(
            default=RouteEntry(runner="claude-code", model="claude-opus-5")
        )
    )
    monkeypatch.setattr("kew.service.run_requests.load_config", lambda **_k: cfg)


def test_a_model_on_another_runner_is_refused_with_a_reason(
    client: Any, claude_route: None
) -> None:
    c, db = client
    _live_drive(db)
    r = c.post(
        "/api/run-requests",
        json={"command": DISPATCH, "item_id": "42", "model": "gpt-5.6-sol"},
        headers=_login(c),
    )
    assert r.status_code == 422
    assert "runs on codex, but this dispatch runs on claude-code" in r.json()["detail"]
    assert _requests(db) == []


@pytest.mark.parametrize(
    ("command", "model", "why"),
    [
        (DISPATCH, "claude-nonesuch-9", "not a model id in the catalog"),
        (DISPATCH, "claude-opus-4-7", "not routable"),
        (UNHOLD, "claude-sonnet-5-5", "takes no model"),
    ],
)
def test_any_other_model_is_refused(
    client: Any, claude_route: None, command: str, model: str, why: str
) -> None:
    c, db = client
    _live_drive(db)
    r = c.post(
        "/api/run-requests",
        json={"command": command, "model": model},
        headers=_login(c),
    )
    assert r.status_code == 422
    assert why in r.json()["detail"]
    assert _requests(db) == []


def test_a_matching_model_rides_the_request(client: Any, claude_route: None) -> None:
    c, db = client
    _live_drive(db)
    plain = c.post(
        "/api/run-requests", json={"command": DISPATCH}, headers=_login(c)
    ).json()
    r = c.post(
        "/api/run-requests",
        json={"command": DISPATCH, "item_id": "42", "model": "claude-sonnet-5-5"},
        headers=_login(c),
    )
    assert r.status_code == 200
    body = r.json()
    # The same command with a model is its own request, not the live plain one.
    assert body["created"] is True
    assert body["request"]["id"] != plain["request"]["id"]
    assert body["request"]["model"] == "claude-sonnet-5-5"
    assert plain["request"]["model"] is None
    with SqliteRunStore(db) as store:
        queued = store.list_audit_events(event="run_request_queued", limit=None)
    assert sorted(str(e.payload.get("model")) for e in queued) == [
        "None",
        "claude-sonnet-5-5",
    ]


def test_a_model_filter_finds_its_request_behind_a_full_page_of_others(
    client: Any,
) -> None:
    """#2878: the list filters by model before its limit, so the selected
    model's queued request behind more than a page of newer requests for other
    models still reaches the Run button."""
    c, db = client
    start = datetime.now(UTC) - timedelta(hours=1)
    with SqliteRunStore(db) as store:
        sol, _ = rr.queue(
            store, DISPATCH, operator="tom", source="web", model="sol", now=start
        )
        plain, _ = rr.queue(store, DISPATCH, operator="tom", source="web", now=start)
        for n in range(LIST_LIMIT + 1):
            rr.queue(
                store,
                DISPATCH,
                operator="tom",
                source="web",
                model=f"other-{n}",
                now=start + timedelta(seconds=n + 1),
            )
    unfiltered = c.get("/api/run-requests", params={"command": DISPATCH}).json()
    assert len(unfiltered["requests"]) == LIST_LIMIT
    assert sol.id not in [r["id"] for r in unfiltered["requests"]]

    by_model = c.get(
        "/api/run-requests", params={"command": DISPATCH, "model": "sol"}
    ).json()
    assert [(r["id"], r["state"]) for r in by_model["requests"]] == [(sol.id, "queued")]
    # An empty model is a request sent without one.
    no_model = c.get(
        "/api/run-requests", params={"command": DISPATCH, "model": ""}
    ).json()
    assert [r["id"] for r in no_model["requests"]] == [plain.id]


# --------------------------------------------------------------------------- #
# A submission key (#2975)
# --------------------------------------------------------------------------- #

KEY = "ab" * 16  # a fake submission key, low entropy so the leak scan stays quiet


def test_a_lost_answer_asked_again_returns_the_same_request(client: Any) -> None:
    c, db = client
    _live_drive(db)
    headers = _login(c)
    body = {"command": UNHOLD, "item_id": "12", "key": KEY}

    first = c.post("/api/run-requests", json=body, headers=headers)
    again = c.post("/api/run-requests", json=body, headers=headers)

    assert (first.status_code, again.status_code) == (200, 200)
    assert again.json()["request"]["id"] == first.json()["request"]["id"]
    assert again.json()["created"] is False
    assert len(_requests(db)) == 1


def test_a_key_sent_with_another_command_is_409(client: Any) -> None:
    c, db = client
    _live_drive(db)
    headers = _login(c)
    first = c.post(
        "/api/run-requests", json={"command": UNHOLD, "key": KEY}, headers=headers
    )

    reused = c.post(
        "/api/run-requests",
        json={"command": "kew epic unhold epic:a 13", "key": KEY},
        headers=headers,
    )

    assert (first.status_code, reused.status_code) == (200, 409)
    assert len(_requests(db)) == 1


def test_a_pair_still_queued_refuses_another_press_with_409(client: Any) -> None:
    c, db = client
    _live_drive(db)
    headers = _login(c)
    first = c.post(
        "/api/run-requests", json={"command": UNHOLD, "key": KEY}, headers=headers
    )
    second = c.post(
        "/api/run-requests",
        json={"command": UNHOLD, "key": "a" * 32},
        headers=headers,
    )

    assert (first.status_code, second.status_code) == (200, 409)
    assert first.json()["request"]["id"] in second.json()["detail"]
    # The request holding the pair rides the refusal, for the press to follow.
    assert second.json()["request"]["id"] == first.json()["request"]["id"]
    assert len(_requests(db)) == 1


def test_a_stale_queued_request_under_one_key_refuses_another_key(
    client: Any,
) -> None:
    """A request left queued past QUEUE_TTL still holds its pair until the
    drive refuses it: a second key is 409, and no second row is written."""
    c, db = client
    _live_drive(db)
    old = datetime.now(UTC) - rr.QUEUE_TTL - timedelta(hours=1)
    with SqliteRunStore(db) as store:
        stale, _ = rr.queue(
            store, UNHOLD, operator="tom", source="web", key=KEY, now=old
        )

    second = c.post(
        "/api/run-requests",
        json={"command": UNHOLD, "key": "a" * 32},
        headers=_login(c),
    )

    assert second.status_code == 409
    assert second.json()["request"]["id"] == stale.id
    assert [r.id for r in _requests(db)] == [stale.id]


def test_a_request_running_past_the_list_window_is_its_pairs_lookup(
    client: Any,
) -> None:
    """The console gates Run and Copy on the pair lookup, so the request
    holding the pair comes back however long ago it was queued."""
    c, db = client
    long_ago = datetime.now(UTC) - LIST_WINDOW - timedelta(days=1)
    with SqliteRunStore(db) as store:
        queued, _ = rr.queue(store, UNHOLD, operator="tom", source="web", now=long_ago)
        running = rr.claim(store, queued, drive="d", host="h", log="x", now=long_ago)
        assert running is not None

    pair = c.get("/api/run-requests", params={"command": UNHOLD, "model": ""}).json()
    other_model = c.get(
        "/api/run-requests", params={"command": UNHOLD, "model": "sol"}
    ).json()

    assert [(r["id"], r["state"]) for r in pair["requests"]] == [(queued.id, "running")]
    assert other_model["requests"] == []


@pytest.mark.parametrize("key", ["short", "x" * 65, "a b" * 8, "../" * 8])
def test_a_key_that_is_not_a_plain_token_is_422(client: Any, key: str) -> None:
    c, db = client
    _live_drive(db)
    r = c.post(
        "/api/run-requests", json={"command": UNHOLD, "key": key}, headers=_login(c)
    )
    assert r.status_code == 422
    assert _requests(db) == []


def test_a_press_whose_fact_fails_is_424_with_nothing_queued(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    c, db = client
    _live_drive(db)
    headers = _login(c)
    insert = SqliteRunStore._chained_insert_in_transaction

    def failing(self: SqliteRunStore, values: Any, **kwargs: Any) -> Any:
        if values[1] == "run_request_queued":
            raise OSError("the audit table is locked")
        return insert(self, values, **kwargs)

    monkeypatch.setattr(SqliteRunStore, "_chained_insert_in_transaction", failing)
    body = {"command": UNHOLD, "item_id": "12", "key": KEY}

    refused = [c.post("/api/run-requests", json=body, headers=headers) for _ in "ab"]
    assert [r.status_code for r in refused] == [424, 424]
    assert refused[0].json()["detail"] == rr.UNAUDITED_DETAIL
    assert _requests(db) == []

    monkeypatch.undo()
    queued = c.post("/api/run-requests", json=body, headers=headers)
    assert queued.status_code == 200 and queued.json()["created"] is True
    assert [r.id for r in _requests(db)] == [queued.json()["request"]["id"]]
