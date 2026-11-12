"""Tests for the epic DAG read API (api_epics router, #454 Phase B).

Router-level read_guard (audit F7 regression), bounds+membership identifier
validation, hostile-ref/node_id tests, no require_write import, typed SSE
data-only frames. Seeds a real SqliteRunStore the same way
test_service_epics.py does (append_epic_tick/begin_epic_tick/finish_epic_tick
+ RunRecord rows) so the whole stack — router, service, store — is exercised.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from support import route_deps, source_ast
from support.proxied_stream import read_proxied_stream, session_clock

import kew.serve.api_epics as api_epics_module
from kew.config import DispatchConfig, ProjectConfig
from kew.run_store import RunRecord, SqliteRunStore
from kew.serve.app import create_app
from kew.serve.auth import SessionStore
from kew.serve.security import read_guard, require_write


def _snapshot(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "schema": 1,
        "epic_ref": "e",
        "signal": "progressing",
        "counts": {"todo": 1},
        "frontier": [],
        "nodes": [
            {
                "id": "101",
                "title": "issue 101",
                "status": "todo",
                "status_since": "2026-07-02T00:00:00+00:00",
                "url": None,
                "pr_url": None,
                "external": False,
                "stuck": None,
            }
        ],
        "edges": [],
    }
    base.update(overrides)
    return base


def _actions() -> dict[str, Any]:
    return {"schema": 1, "dispatched": [], "merged": [], "held": [], "stuck": []}


def _seed(db: Path) -> None:
    store = SqliteRunStore(str(db))
    try:
        tick_id, _ = store.append_epic_tick(
            epic_ref="e",
            tick_ts="2026-07-02T00:00:00+00:00",
            snapshot=_snapshot(),
            actions=_actions(),
            snapshot_hash="h1",
        )
        store.begin_epic_tick("e", "2026-07-02T00:00:00+00:00")
        store.finish_epic_tick(
            "e",
            tick_ts="2026-07-02T00:00:00+00:00",
            duration_s=1.0,
            interval_s=30.0,
            signal="progressing",
            tick_id=tick_id,
        )
        # Three tick ATTEMPTS, one of them still open (#2304): the strip reads
        # these, never the snapshots above.
        for outcome, error_class in (("dispatched", None), ("failed", "HTTPError")):
            row = store.begin_epic_tick_log("e", "2026-07-02T00:00:00+00:00")
            store.finish_epic_tick_log(
                row,
                ts_finished="2026-07-02T00:00:01+00:00",
                outcome=outcome,
                signal="progressing",
                n_dispatched=1 if outcome == "dispatched" else 0,
                error_class=error_class,
            )
        store.begin_epic_tick_log("e", "2026-07-02T00:00:02+00:00")
        store.save(
            RunRecord(
                run_id="r1",
                issue_num=101,
                branch="issue-101",
                status="success",
                exit_code=0,
                started_at=datetime(2026, 7, 2, tzinfo=UTC),
                finished_at=datetime(2026, 7, 2, 0, 5, tzinfo=UTC),
                estimated_cost_usd=1.5,
            )
        )
    finally:
        store.close()


def _mk_app(tmp_path: Path, *, expose: bool = False, token: str = "TESTTOKEN") -> Any:
    db = tmp_path / "dispatch.db"
    _seed(db)
    return create_app(
        lambda: SqliteRunStore(str(db)),
        repo_dir=tmp_path,
        token=token,
        sessions=SessionStore(),
        expose=expose,
    )


READ_PATHS = [
    "/api/epics",
    "/api/epics/e",
    "/api/epics/e/ticks?after=0",
    "/api/epics/e/tick-strip",
    "/api/epics/e/nodes/101",
    "/api/epics/e/timeline",
]


def _stream_status(client: Any, path: str) -> int:
    with client.stream("GET", path) as r:
        return r.status_code


def test_list_and_detail_and_ticks_200(tmp_path: Path, inbox_client: Any) -> None:
    app = _mk_app(tmp_path)
    client = inbox_client(app, loopback_peer=True)

    epics = client.get("/api/epics")
    assert epics.status_code == 200
    assert [e["epic_ref"] for e in epics.json()] == ["e"]

    detail = client.get("/api/epics/e")
    assert detail.status_code == 200
    body = detail.json()
    assert body["snapshot"]["nodes"][0]["id"] == "101"

    ticks = client.get("/api/epics/e/ticks?after=0")
    assert ticks.status_code == 200
    assert len(ticks.json()) == 1


def test_ticks_negative_limit_and_after_rejected(
    tmp_path: Path, inbox_client: Any
) -> None:
    """`?limit=-1` must never reach SQLite as `LIMIT -1` (unbounded) —
    FastAPI's `Query(..., ge=1, le=1000)` rejects it with 422 before the
    handler runs. `?after=-1` is clamped the same way (`ge=0`)."""
    app = _mk_app(tmp_path)
    client = inbox_client(app, loopback_peer=True)

    assert client.get("/api/epics/e/ticks?limit=-1").status_code == 422
    assert client.get("/api/epics/e/ticks?after=-1").status_code == 422

    ok = client.get("/api/epics/e/ticks?limit=2")
    assert ok.status_code == 200
    assert len(ok.json()) <= 2


def test_tick_strip_is_bounded_and_reads_attempts_not_snapshots(
    tmp_path: Path, inbox_client: Any
) -> None:
    """The strip read is its own endpoint (#2304).

    `/ticks` returns full DAG snapshots; building a strip there would parse
    two hundred of them to colour two hundred slots. So the strip has its own
    bounded route, and it shows the attempts `/ticks` cannot — including the
    failure and the tick still running.
    """
    app = _mk_app(tmp_path)
    client = inbox_client(app, loopback_peer=True)

    strip = client.get("/api/epics/e/tick-strip")
    assert strip.status_code == 200
    rows = strip.json()
    assert [r["outcome"] for r in rows] == ["dispatched", "failed", None]
    assert rows[1]["error_class"] == "HTTPError"
    assert rows[0]["n_dispatched"] == 1
    # An open row is the live tick: no finish stamp, no outcome.
    assert rows[2]["ts_finished"] is None
    # No snapshot on the wire — that is the whole point of the separate read.
    assert "snapshot" not in rows[0]

    assert client.get("/api/epics/e/tick-strip?limit=-1").status_code == 422
    assert client.get("/api/epics/e/tick-strip?limit=0").status_code == 422
    assert client.get("/api/epics/e/tick-strip?limit=10000").status_code == 422
    bounded = client.get("/api/epics/e/tick-strip?limit=2")
    assert bounded.status_code == 200
    # The newest attempts, not the oldest: a strip shows the recent past.
    assert [r["outcome"] for r in bounded.json()] == ["failed", None]

    # A hostile or unknown ref is 404 here exactly as on every other route.
    assert client.get("/api/epics/..%2Fetc/tick-strip").status_code == 404
    assert client.get("/api/epics/nope/tick-strip").status_code == 404


def test_tick_strip_serves_only_the_marks_a_change_strip_draws(
    tmp_path: Path, inbox_client: Any
) -> None:
    """`?changes=true` (#2844 D23): the Epics list's sparkline draws changes,
    failed ticks and the live tick, so it asks for those rows alone. The seeded
    dispatched attempt journaled nothing and is left out. The seeded journal
    row is a change no attempt carries, so it is drawn off the journal itself
    (#2882): CHANGE counts it, and the strip draws one mark for it."""
    app = _mk_app(tmp_path)
    client = inbox_client(app, loopback_peer=True)

    marks = client.get("/api/epics/e/tick-strip?changes=true")
    assert marks.status_code == 200
    assert [r["outcome"] for r in marks.json()] == ["nothing", "failed", None]
    assert [r["journal_tick_id"] for r in marks.json()] == [1, None, None]
    assert client.get("/api/epics/e/tick-strip?changes=true&limit=0").status_code == 422


def test_the_roster_and_the_heartbeat_both_carry_the_attempt_stamp(
    tmp_path: Path, inbox_client: Any
) -> None:
    """Reviewer finding (PR #2438, round 3), on the wire.

    Both strip readers re-read on a stamp, and the only stamps the payloads
    carried were written by ``begin_epic_tick`` — past the lock, the preflight
    and the non-empty-graph guard. A locked-out, preflight-failing or
    empty-graph driver moved none of them, so the strip stayed frozen through
    exactly the attempts it exists to show.
    """
    app = _mk_app(tmp_path)
    client = inbox_client(app, loopback_peer=True)

    row = client.get("/api/epics").json()[0]
    beat = client.get("/api/epics/e").json()["heartbeat"]
    # One reading, two surfaces: the roster's batched read and the epic page's
    # point read must agree or they re-read on different signals.
    assert row["tick_attempt_stamp"] == beat["tick_attempt_stamp"]
    assert row["tick_attempt_stamp"] != ""

    # One more attempt, of the kind no heartbeat column records.
    with SqliteRunStore(str(tmp_path / "dispatch.db")) as store:
        store.begin_epic_tick_log("e", "2026-07-02T00:01:00+00:00")

    moved = client.get("/api/epics").json()[0]
    assert moved["tick_attempt_stamp"] != row["tick_attempt_stamp"]
    assert moved["last_tick_started_at"] == row["last_tick_started_at"]
    assert moved["last_tick_ts"] == row["last_tick_ts"]


def test_an_attempt_only_epic_is_reachable(tmp_path: Path, inbox_client: Any) -> None:
    """Reviewer finding (PR #2438, round 4), on the wire.

    An epic whose every tick was refused before ``begin_epic_tick`` — failed
    preflight, locked out — has attempt rows and no heartbeat and no journal.
    ``list_epic_refs`` enumerated only the latter two, so those rows had no
    reader anywhere: this route 404'd through ``known_epic_refs`` and the
    roster left the epic out entirely.

    An ``empty_graph``-only ref is the one exclusion (PR #2438 review) and is
    pinned below, because that outcome says the ref never named an epic.
    """
    app = _mk_app(tmp_path)
    client = inbox_client(app, loopback_peer=True)

    with SqliteRunStore(str(tmp_path / "dispatch.db")) as store:
        row = store.begin_epic_tick_log("ghost", "2026-07-02T00:00:00+00:00")
        store.finish_epic_tick_log(
            row,
            ts_finished="2026-07-02T00:00:01+00:00",
            outcome="failed",
            error_class="FileNotFoundError",
        )

    strip = client.get("/api/epics/ghost/tick-strip")
    assert strip.status_code == 200
    assert [r["outcome"] for r in strip.json()] == ["failed"]

    roster = {e["epic_ref"]: e for e in client.get("/api/epics").json()}
    assert "ghost" in roster
    # Known, with nothing a refused tick could have recorded — the same shape a
    # registered-but-never-driven epic serves.
    assert roster["ghost"]["signal"] is None
    assert roster["ghost"]["counts"] == {}
    assert roster["ghost"]["tick_attempt_stamp"] != ""
    assert client.get("/api/epics/ghost").status_code == 200


def test_an_empty_graph_only_ref_is_not_served_as_an_epic(
    tmp_path: Path, inbox_client: Any
) -> None:
    """Reviewer finding (PR #2438), on the wire. A mistyped
    ``kew epic advance`` name leaves one ``empty_graph`` attempt row and
    nothing else, and that outcome means the tracker query matched no work at
    all — so the ref is a typo, not an epic. It must not join the roster the
    console lists and ``kew epic run --all`` drives.
    """
    app = _mk_app(tmp_path)
    client = inbox_client(app, loopback_peer=True)

    with SqliteRunStore(str(tmp_path / "dispatch.db")) as store:
        store.register_epic("real-epic")
        row = store.begin_epic_tick_log("typoo", "2026-07-02T00:00:00+00:00")
        store.finish_epic_tick_log(
            row,
            ts_finished="2026-07-02T00:00:01+00:00",
            outcome="empty_graph",
            error_class="EmptyEpicGraphError",
        )

    roster = [e["epic_ref"] for e in client.get("/api/epics").json()]
    assert "real-epic" in roster
    assert "typoo" not in roster
    assert client.get("/api/epics/typoo").status_code == 404
    assert client.get("/api/epics/typoo/tick-strip").status_code == 404


@pytest.mark.policy
def test_the_ticks_route_never_builds_the_strip() -> None:
    """`/ticks` must not grow a strip (#2304 spec).

    The rule is about what the handler is allowed to call, which no response
    assertion can pin, so it is read off the handler's syntax tree: no name it
    spells in code may reach the strip or its log read.
    """
    handler = source_ast.definition(api_epics_module, "ticks")
    named = source_ast.identifiers(handler)
    assert not {name for name in named if "tick_strip" in name}, named
    assert "list_epic_tick_log_tail" not in named


_STATE_TABLES = ("epic_ticks", "epic_driver_state", "runs", "audit_events")


def _row_counts(db: Path) -> dict[str, int]:
    """Row counts of every state table, via a read-only connection."""
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return {
            t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]  # noqa: S608
            for t in _STATE_TABLES
        }
    finally:
        conn.close()


@pytest.mark.parametrize(
    "ref",
    [
        # Brief-mandated hostile matrix. Percent-encoded forms are sent as-is
        # (httpx preserves valid %XX sequences) so the server-side DECODED
        # value is what hits the validator.
        "../etc",  # dot-segment traversal shape
        # encoded slash -> decodes to "../etc"; rejected at the routing
        # layer or the validator, depending on how/where decoding happens
        "..%2Fetc",
        "a%00b",  # encoded NUL -> decodes to "a\x00b"
        "A" * 1001,  # far past the 100-char cap
        "a b",  # space (httpx encodes to %20; decodes back to "a b")
        "a/b",  # literal slash -> extra path segment, matches no epics route
        "ref!",  # charset violation
        # Extras kept from the original matrix:
        "a" * 200,
        "bad ref!",
        "unknown-epic",  # charset-valid but not a known ref (membership check)
    ],
)
def test_hostile_and_unknown_epic_ref_404(
    tmp_path: Path, inbox_client: Any, ref: str
) -> None:
    app = _mk_app(tmp_path)
    client = inbox_client(app, loopback_peer=True)
    db = tmp_path / "dispatch.db"
    before = _row_counts(db)
    assert client.get(f"/api/epics/{ref}").status_code == 404
    assert client.get(f"/api/epics/{ref}/ticks").status_code == 404
    assert client.get(f"/api/epics/{ref}/nodes/101").status_code == 404
    # Read-only surface: hostile input must never create/mutate state rows.
    assert _row_counts(db) == before


def test_empty_epic_ref_never_matches_a_ref_route(
    tmp_path: Path, inbox_client: Any
) -> None:
    """The brief's "" hostile ref, documented per URL shape it produces.

    An empty path segment can't match Starlette's `{epic_ref}` ([^/]+), so
    ticks/nodes URLs with "" 404. The bare detail URL becomes "/api/epics/"
    which is the list route's trailing-slash redirect — that resolves to the
    (authorized) list endpoint, never to any per-ref resource, so the intent
    holds: no 200-with-per-ref-data, no error besides 404/405.
    """
    app = _mk_app(tmp_path)
    client = inbox_client(app, loopback_peer=True)
    db = tmp_path / "dispatch.db"
    before = _row_counts(db)

    assert client.get("/api/epics//ticks").status_code == 404
    assert client.get("/api/epics//nodes/101").status_code == 404

    detail = client.get("/api/epics/", follow_redirects=False)
    assert detail.status_code in (307, 404)
    if detail.status_code == 307:
        # Trailing-slash redirect to the list endpoint, not a ref resource.
        assert detail.headers["location"].rstrip("/").endswith("/api/epics")

    assert _row_counts(db) == before


@pytest.mark.parametrize("node_id", ["abc", "1e5", "-1", "1" * 20])
def test_hostile_node_id_404(tmp_path: Path, inbox_client: Any, node_id: str) -> None:
    app = _mk_app(tmp_path)
    client = inbox_client(app, loopback_peer=True)
    assert client.get(f"/api/epics/e/nodes/{node_id}").status_code == 404


@pytest.mark.parametrize("path", READ_PATHS)
def test_exposed_reads_require_session(
    tmp_path: Path, inbox_client: Any, path: str
) -> None:
    app = _mk_app(tmp_path, expose=True)
    client = inbox_client(app, loopback_peer=False)
    assert _stream_status(client, path) == 401


def test_stream_requires_session_when_exposed(
    tmp_path: Path, inbox_client: Any
) -> None:
    app = _mk_app(tmp_path, expose=True)
    client = inbox_client(app, loopback_peer=False)
    assert _stream_status(client, "/api/epics/e/stream") == 401


def test_loopback_reads_ungated_when_not_exposed(
    tmp_path: Path, inbox_client: Any
) -> None:
    app = _mk_app(tmp_path, expose=False)
    client = inbox_client(app, loopback_peer=True)
    for path in READ_PATHS:
        assert _stream_status(client, path) == 200


# Real wall clock (the stream's deadline). Serial phase: see tests/conftest.py.
@pytest.mark.serial
@pytest.mark.asyncio
async def test_a_proxied_epic_stream_closes_when_its_session_expires(
    tmp_path: Path,
) -> None:
    """The opening gate asked a proxied loopback reader for a session, so the
    stream's per-poll re-check must too: an expired session ends it at once
    instead of after the 30-minute cap."""
    db = tmp_path / "dispatch.db"
    _seed(db)
    # create + the opening gate see a live session; every poll after, expired.
    sessions = SessionStore(ttl_seconds=100.0, now=session_clock(2, expired_at=101.0))
    app = create_app(
        lambda: SqliteRunStore(str(db)),
        repo_dir=tmp_path,
        token="TESTTOKEN",
        sessions=sessions,
        expose=False,
    )
    sid, _ = sessions.create()

    response, open_s = await read_proxied_stream(
        app, "/api/epics/e/stream", session_id=sid
    )

    assert response.status_code == 200
    assert open_s < 5.0, "the stream outlived its session"


def test_no_token_via_query_or_fragment(tmp_path: Path, inbox_client: Any) -> None:
    app = _mk_app(tmp_path, expose=True, token="realtoken123")
    client = inbox_client(app, loopback_peer=False)
    for path in [*READ_PATHS, "/api/epics/e/stream"]:
        gated = path + ("&" if "?" in path else "?") + "token=realtoken123"
        assert _stream_status(client, gated) == 401
    # The stream's 401 must be the guard's JSON rejection, never an opened
    # event-stream body (the generator is never entered on auth failure).
    r = client.get("/api/epics/e/stream?token=realtoken123")
    assert r.status_code == 401
    assert "text/event-stream" not in r.headers.get("content-type", "")


def test_no_route_takes_a_write_session_or_a_greedy_path() -> None:
    """Read-only and guarded: every route resolves ``read_guard``, none resolves
    ``require_write``, and no path parameter uses the greedy ``:path``
    converter that would let an epic ref swallow the route suffix."""
    for route in route_deps.api_routes(api_epics_module.router):
        resolved = route_deps.dependency_calls(route)
        assert read_guard in resolved, route.path
        assert require_write not in resolved, route.path
        assert ":path" not in route.path, route.path


def test_stream_emits_typed_data_frames(
    tmp_path: Path, inbox_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # TestClient's transport (both the sync test transport and httpx's async
    # ASGITransport) fully drives the ASGI call to completion before handing
    # anything back to the caller — a truly unbounded stream (the route's
    # default MAX_STREAM_SECONDS of 1800s) can never be drained inside a
    # test. Bound this one connection so the call completes fast, then
    # assert on the fully-buffered body; the frame *content* this proves is
    # identical to what a real (never-ending) connection would emit first.
    real_epic_stream = api_epics_module.epic_stream

    def _bounded_stream(hub: Any, ref: str, *, is_authorized: Any) -> Any:
        return real_epic_stream(
            hub, ref, is_authorized=is_authorized, poll_s=0.01, max_seconds=0.2
        )

    monkeypatch.setattr(api_epics_module, "epic_stream", _bounded_stream)

    app = _mk_app(tmp_path)
    client = inbox_client(app, loopback_peer=True)
    r = client.get("/api/epics/e/stream")
    assert r.status_code == 200
    assert "data: {" in r.text
    assert '"type": "snapshot"' in r.text
    assert "event:" not in r.text


def test_node_detail_and_hostile_node_id(tmp_path: Path, inbox_client: Any) -> None:
    app = _mk_app(tmp_path)
    client = inbox_client(app, loopback_peer=True)

    ok = client.get("/api/epics/e/nodes/101")
    assert ok.status_code == 200
    body = ok.json()
    assert body["lifetime_usd"] == 1.5
    assert len(body["runs"]) == 1

    assert client.get("/api/epics/e/nodes/abc").status_code == 404


# --- I02: opaque epic-node ids through the API boundary ---------------------


def _sfd_node(node_id: str) -> dict[str, Any]:
    return {
        "id": node_id,
        "title": f"issue {node_id}",
        "status": "todo",
        "status_since": "2026-07-02T00:00:00+00:00",
        "url": None,
        "pr_url": None,
        "external": False,
        "stuck": None,
    }


def _seed_opaque(db: Path) -> None:
    """An epic whose members are a Linear-shaped id and a numeric one."""
    store = SqliteRunStore(str(db))
    try:
        store.append_epic_tick(
            epic_ref="e",
            tick_ts="2026-07-02T00:00:00+00:00",
            snapshot=_snapshot(
                nodes=[
                    _sfd_node("SFD-12"),
                    _sfd_node("TEAM:12"),
                    _sfd_node("101"),
                ]
            ),
            actions=_actions(),
            snapshot_hash="h-opaque",
        )
        store.save(
            RunRecord(
                run_id="r-sfd",
                issue_num=0,  # 0 sentinel: the real key is item_id
                item_id="SFD-12",
                branch="agent/issue-sfd-12",
                status="success",
                exit_code=0,
                started_at=datetime(2026, 7, 2, tzinfo=UTC),
                finished_at=datetime(2026, 7, 2, 0, 5, tzinfo=UTC),
                estimated_cost_usd=2.5,
            )
        )
    finally:
        store.close()


def test_opaque_member_node_is_served_not_rejected_for_being_nonnumeric(
    tmp_path: Path, inbox_client: Any
) -> None:
    db = tmp_path / "dispatch.db"
    _seed_opaque(db)
    app = create_app(
        lambda: SqliteRunStore(str(db)),
        repo_dir=tmp_path,
        token="TESTTOKEN",
        sessions=SessionStore(),
        expose=False,
    )
    client = inbox_client(app, loopback_peer=True)

    resp = client.get("/api/epics/e/nodes/SFD-12")

    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == "SFD-12"
    assert body["lifetime_usd"] == 2.5
    assert len(body["runs"]) == 1


def test_exact_member_with_punctuation_is_served(
    tmp_path: Path, inbox_client: Any
) -> None:
    db = tmp_path / "dispatch.db"
    _seed_opaque(db)
    app = create_app(
        lambda: SqliteRunStore(str(db)),
        repo_dir=tmp_path,
        token="TESTTOKEN",
        sessions=SessionStore(),
        expose=False,
    )
    client = inbox_client(app, loopback_peer=True)

    resp = client.get("/api/epics/e/nodes/TEAM:12")

    assert resp.status_code == 200
    assert resp.json()["id"] == "TEAM:12"


@pytest.mark.parametrize(
    "node_id",
    [
        "SFD-99",  # well-formed, but not a member of this epic
        "SFD-12x",  # near-miss of a real member
        ".hidden",  # leading dot: not an id shape
        "%2e%2e",  # percent-encoded traversal, decoded to ".." before the handler
        "%2e%2e%2f",  # percent-encoded traversal with a separator
        "a%2Fb",  # percent-encoded separator: decodes to "a/b" after routing
        "x" * 65,  # over the id bound
        "x" * 300,
    ],
)
def test_nonmember_and_path_shaped_node_ids_404(
    tmp_path: Path, inbox_client: Any, node_id: str
) -> None:
    db = tmp_path / "dispatch.db"
    _seed_opaque(db)
    app = create_app(
        lambda: SqliteRunStore(str(db)),
        repo_dir=tmp_path,
        token="TESTTOKEN",
        sessions=SessionStore(),
        expose=False,
    )
    client = inbox_client(app, loopback_peer=True)
    before = _row_counts(db)

    assert client.get(f"/api/epics/e/nodes/{node_id}").status_code == 404
    # Read-only surface: a rejected id must never create or mutate state.
    assert _row_counts(db) == before


# --------------------------------------------------------------------------- #
# FUI-10 — needs-you (attention) and blocked reason over the wire
# --------------------------------------------------------------------------- #


def _awaiting_node() -> dict[str, Any]:
    return {
        "id": "2055",
        "title": "issue 2055",
        "status": "in_review",
        "status_since": "2026-09-24T00:00:00+00:00",
        "url": None,
        "pr_url": "https://example.invalid/pr/2059",
        "external": False,
        "stuck": None,
    }


def _blocked_node() -> dict[str, Any]:
    return {
        "id": "2053",
        "title": "issue 2053",
        "status": "blocked",
        "status_since": "2026-09-24T00:00:00+00:00",
        "url": None,
        "pr_url": None,
        "external": False,
        "stuck": "needs a design decision",
    }


def _seed_needs_you(db: Path) -> None:
    store = SqliteRunStore(str(db))
    try:
        store.append_epic_tick(
            epic_ref="e",
            tick_ts="2026-09-24T00:00:00+00:00",
            snapshot=_snapshot(nodes=[_awaiting_node(), _blocked_node()]),
            actions=_actions(),
            snapshot_hash="h-needs-you",
        )
        store.save(
            RunRecord(
                run_id="r-2055",
                issue_num=2055,
                item_id="2055",
                branch="agent/issue-2055",
                status="success",
                exit_code=0,
                started_at=datetime(2026, 9, 24, tzinfo=UTC),
                finished_at=datetime(2026, 9, 24, 0, 30, tzinfo=UTC),
                pr_url="https://example.invalid/pr/2059",
                pr_state="OPEN",
            )
        )
    finally:
        store.close()


def _needs_you_app(tmp_path: Path, *, config: Any) -> Any:
    db = tmp_path / "dispatch.db"
    _seed_needs_you(db)
    return create_app(
        lambda: SqliteRunStore(str(db)),
        repo_dir=tmp_path,
        config=config,
        token="TESTTOKEN",
        sessions=SessionStore(),
    )


def test_epic_detail_serves_attention_and_blocked_reason(
    tmp_path: Path, inbox_client: Any
) -> None:
    """The read-guarded detail route carries the needs-you join, and the
    blocked line is the driver's own words."""
    app = _needs_you_app(
        tmp_path, config=DispatchConfig(project=ProjectConfig(repo="acme/widgets"))
    )
    client = inbox_client(app, loopback_peer=True)

    body = client.get("/api/epics/e").json()

    nodes = {n["id"]: n for n in body["snapshot"]["nodes"]}
    assert nodes["2055"]["attention"] == {
        "kind": "merge_decision",
        "reason": "PR open for #2055 — awaiting review",
        "url": "https://example.invalid/pr/2059",
        # The projection's own kinds name no single next move: the artifact IS
        # the move, and the rail's call to action is a link to it. Only the
        # epic's journaled waits carry an action (#2207).
        "action": "",
        # And no kew command makes a merge decision — it happens on the host.
        # The popover prints THIS field and nothing read out of `action`
        # (#2250), so an empty one here is an empty command line on screen.
        "command": "",
        # Its move is the link instead, and the wait is said in the one
        # sentence the needs-you row for #2055 carries too (#2539).
        "sentence": "PR #2059 is open — the merge decision is yours",
        "link": {
            "label": "Open PR #2059 to merge",
            "href": "https://example.invalid/pr/2059",
        },
        # No merge verify failed at its head, so the PR is its only move (#3460).
        "verify_link": None,
    }
    assert nodes["2053"]["attention"] is None
    assert nodes["2053"]["blocked_reason"] == "needs a design decision"
    assert nodes["2055"]["blocked_reason"] is None
    assert body["snapshot"]["unavailable"] == []


def test_epic_detail_serves_a_journaled_wait_s_command_already_filled(
    tmp_path: Path, inbox_client: Any
) -> None:
    """The DAG popover's Copy button prints THIS field, verbatim (#2250).

    Its first round composed the command in the browser out of ``action``, and
    ``action`` is ``kew.waits``' prose — ``kew epic continue <name> <node>``
    here — so the line under the Copy button was the placeholders themselves.
    The route carries the filled command instead, with this epic's ref and this
    node's id in it, and nothing on the wire has to be parsed to get it.
    """
    db = tmp_path / "dispatch.db"
    store = SqliteRunStore(str(db))
    try:
        store.append_epic_tick(
            epic_ref="e",
            tick_ts="2026-09-24T00:00:00+00:00",
            snapshot=_snapshot(
                nodes=[{**_awaiting_node(), "status": "awaiting_continuation"}]
            ),
            actions=_actions(),
            snapshot_hash="h-continue",
        )
    finally:
        store.close()
    app = create_app(
        lambda: SqliteRunStore(str(db)),
        repo_dir=tmp_path,
        config=DispatchConfig(project=ProjectConfig(repo="acme/widgets")),
        token="TESTTOKEN",
        sessions=SessionStore(),
    )
    client = inbox_client(app, loopback_peer=True)

    body = client.get("/api/epics/e").json()

    (node,) = body["snapshot"]["nodes"]
    assert node["attention"]["action"] == "kew epic continue <name> <node>"
    assert node["attention"]["command"] == "kew epic continue e 2055"
    assert "<" not in node["attention"]["command"]


def test_epic_detail_names_a_join_it_could_not_compose(
    tmp_path: Path, inbox_client: Any
) -> None:
    """No config, no projection — and the payload says so rather than serving
    a DAG on which nobody appears to be waiting."""
    app = _needs_you_app(tmp_path, config=None)
    client = inbox_client(app, loopback_peer=True)

    body = client.get("/api/epics/e").json()

    assert all(n["attention"] is None for n in body["snapshot"]["nodes"])
    assert body["snapshot"]["unavailable"] == ["attention"]


def _bound_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bound one connection so TestClient can drain it (see
    test_stream_emits_typed_data_frames for why an unbounded one cannot be)."""
    real_epic_stream = api_epics_module.epic_stream

    def _bounded(hub: Any, ref: str, *, is_authorized: Any) -> Any:
        return real_epic_stream(
            hub, ref, is_authorized=is_authorized, poll_s=0.01, max_seconds=0.2
        )

    monkeypatch.setattr(api_epics_module, "epic_stream", _bounded)


def _stream_snapshots(client: Any) -> list[dict[str, Any]]:
    r = client.get("/api/epics/e/stream")
    assert r.status_code == 200
    frames = [
        json.loads(line.removeprefix("data: "))
        for line in r.text.splitlines()
        if line.startswith("data: ")
    ]
    return [f["data"]["snapshot"] for f in frames if f["type"] == "snapshot"]


def test_streamed_snapshot_frames_carry_the_same_needs_you_join(
    tmp_path: Path, inbox_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Live replaces the detail route's whole snapshot, so a frame must agree
    with it about the identical tick — the DAG and the judgment queue cannot
    tell two stories just because the epic is ticking."""
    _bound_stream(monkeypatch)
    app = _needs_you_app(
        tmp_path, config=DispatchConfig(project=ProjectConfig(repo="acme/widgets"))
    )
    client = inbox_client(app, loopback_peer=True)

    detail = client.get("/api/epics/e").json()["snapshot"]
    snapshots = _stream_snapshots(client)

    assert snapshots
    assert all(snap == detail for snap in snapshots)
    assert detail["unavailable"] == []


def _stream_frames(client: Any, kind: str) -> list[dict[str, Any]]:
    r = client.get("/api/epics/e/stream")
    assert r.status_code == 200
    frames = [
        json.loads(line.removeprefix("data: "))
        for line in r.text.splitlines()
        if line.startswith("data: ")
    ]
    return [f["data"] for f in frames if f["type"] == kind]


def test_the_stream_carries_the_join_as_its_own_frame(
    tmp_path: Path, inbox_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The join reaches the wire independently of the journal, so a node whose
    attention changes between ticks (a PR merged by serve's own sweep) can be
    refreshed without waiting for the driver to append a row."""
    _bound_stream(monkeypatch)
    app = _needs_you_app(
        tmp_path, config=DispatchConfig(project=ProjectConfig(repo="acme/widgets"))
    )
    client = inbox_client(app, loopback_peer=True)

    detail = client.get("/api/epics/e").json()["snapshot"]
    attention = _stream_frames(client, "attention")

    assert attention
    nodes = {n["id"]: n["attention"] for n in detail["nodes"]}
    assert attention[0] == {
        "by_node": {"2055": nodes["2055"]},
        "unavailable": detail["unavailable"],
    }


def test_streamed_frames_name_a_join_they_could_not_compose(
    tmp_path: Path, inbox_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bound_stream(monkeypatch)
    app = _needs_you_app(tmp_path, config=None)
    client = inbox_client(app, loopback_peer=True)

    snapshots = _stream_snapshots(client)

    assert snapshots
    assert all(snap["unavailable"] == ["attention"] for snap in snapshots)


def test_needs_you_join_is_still_session_gated_under_expose(
    tmp_path: Path, inbox_client: Any
) -> None:
    """The attention join adds no read that escapes read_guard."""
    db = tmp_path / "dispatch.db"
    _seed_needs_you(db)
    app = create_app(
        lambda: SqliteRunStore(str(db)),
        repo_dir=tmp_path,
        config=DispatchConfig(project=ProjectConfig(repo="acme/widgets")),
        token="TESTTOKEN",
        sessions=SessionStore(),
        expose=True,
    )
    client = inbox_client(app, loopback_peer=True)

    assert client.get("/api/epics/e").status_code == 401


def _seed_timeline(db: Path) -> None:
    """The seeded epic, plus the facts a timeline lane is drawn from: one
    status transition, one run keyed on the durable item id, one verdict."""
    store = SqliteRunStore(str(db))
    try:
        store.append_audit_event(
            timestamp="2026-07-02T01:00:00+00:00",
            event="epic_node_status",
            issue_num=101,
            item_id="101",
            actor_type="policy",
            actor_id="driver",
            payload={"epic_ref": "e", "from": "todo", "to": "in_progress"},
        )
        store.save(
            RunRecord(
                run_id="r-timeline",
                issue_num=101,
                item_id="101",
                branch="issue-101",
                status="success",
                exit_code=0,
                started_at=datetime(2026, 7, 2, 1, tzinfo=UTC),
                finished_at=datetime(2026, 7, 2, 1, 30, tzinfo=UTC),
                estimated_cost_usd=0.5,
                pr_merged_at="2026-07-02T02:00:00+00:00",
            )
        )
        store.record_review_round("e", "101", head_sha="abc1234", verdict="READY")
    finally:
        store.close()


def test_timeline_200_and_unknown_epic_404(tmp_path: Path, inbox_client: Any) -> None:
    app = _mk_app(tmp_path)
    _seed_timeline(tmp_path / "dispatch.db")
    client = inbox_client(app, loopback_peer=True)

    r = client.get("/api/epics/e/timeline")
    assert r.status_code == 200
    body = r.json()
    assert body["epic_ref"] == "e"
    assert body["span"]["end"] == body["now"]
    # The base seed's run starts the span; the timeline seed adds to it.
    assert body["span"]["start"] == "2026-07-02T00:00:00+00:00"
    (node,) = body["nodes"]
    assert node["id"] == "101"
    # The wire name is `from`, not the Python-side `from_`.
    assert node["bands"] == [
        {"status": "in_progress", "from": "2026-07-02T01:00:00+00:00", "to": None}
    ]
    assert [r_["run_id"] for r_ in node["runs"]] == ["r1", "r-timeline"]
    assert node["rounds"][0]["verdict"] == "READY"
    assert node["merged_at"] == "2026-07-02T02:00:00+00:00"

    assert client.get("/api/epics/nope/timeline").status_code == 404
