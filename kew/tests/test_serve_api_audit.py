"""GET /api/audit and /api/audit/status endpoint tests.

Mirrors tests/test_serve_api_report.py (read_guard route module) and the
--expose auth gating in tests/test_serve_api_authz.py.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import kew.service.audit as audit_svc
from kew.audit_chain import GENESIS_PREV, ChainRow, entry_hash, format_anchor
from kew.ids import MAX_ISSUE_NUM_DIGITS, SQLITE_INT_MAX
from kew.run_store import SqliteRunStore
from kew.serve.app import create_app


@pytest.fixture(autouse=True)
def _reset_status_cache():
    """Isolate the module-global verify cache between tests (order-independence)."""
    audit_svc._status_cache.clear()
    yield
    audit_svc._status_cache.clear()


def _ev(id_, event, *, actor_type="agent", issue_num=None, payload=None):
    return SimpleNamespace(
        id=id_,
        timestamp=f"2026-07-15T00:0{id_}:00Z",
        event=event,
        run_id=f"r{id_}",
        issue_num=issue_num,
        actor_type=actor_type,
        actor_id="op",
        payload=payload,
    )


class FakeStore:
    """Duck-typed store honoring the event filter the real store supports."""

    def __init__(self, events=None, chain=None):
        self._events = events or []
        self._chain = chain or []
        self.last_list_limit: int | None = None
        self.chain_reads: int = 0

    def list_audit_events(
        self,
        limit=100,
        *,
        event=None,
        event_prefix=None,
        event_prefixes=None,
        item_id=None,
        item_or_issue=None,
        epic_ref=None,
        epic_node_ids=None,
        since=None,
        until=None,
        actor_type=None,
        issue_num=None,
        after_id=None,
    ):
        # The window/item/epic/prefix filters are SQL; what they MEAN is pinned
        # against the real store below, not restated here.
        self.last_list_limit = limit
        rows = [e for e in self._events if event is None or e.event == event]
        if actor_type is not None:
            rows = [r for r in rows if getattr(r, "actor_type", None) == actor_type]
        if issue_num is not None:
            rows = [r for r in rows if getattr(r, "issue_num", None) == issue_num]
        if after_id is not None:
            rows = [r for r in rows if r.id < after_id]
        rows.sort(key=lambda e: e.id, reverse=True)  # newest first
        return rows if limit is None else rows[:limit]

    def read_chain_rows(self):
        self.chain_reads += 1
        return list(self._chain)

    def read_chain_head_key(self):
        if not self._chain:
            return None
        return (len(self._chain), entry_hash(self._chain[-1]))


def _client(store, tmp_path: Path, inbox_client, *, expose=False) -> TestClient:
    @contextmanager
    def factory():
        yield store

    app = create_app(store_factory=factory, repo_dir=tmp_path, expose=expose)
    return inbox_client(app, loopback_peer=True)


def _chain():
    r1 = ChainRow(
        id=1,
        timestamp="2026-07-15T00:00:00Z",
        event="dispatch.start",
        run_id="r1",
        issue_num=613,
        repo="kew",
        actor_type="policy",
        actor_id=None,
        endpoint=None,
        policy_hash=None,
        payload=None,
        prev_hash=GENESIS_PREV,
    )
    r2 = ChainRow(
        id=2,
        timestamp="2026-07-15T00:01:00Z",
        event="approve",
        run_id="r1",
        issue_num=613,
        repo="kew",
        actor_type="human",
        actor_id="op",
        endpoint=None,
        policy_hash=None,
        payload=None,
        prev_hash=entry_hash(r1),
    )
    return [r1, r2]


def test_audit_list_returns_events_newest_first(tmp_path, inbox_client):
    events = [
        _ev(1, "dispatch.start", issue_num=613),
        _ev(2, "approve", actor_type="human", issue_num=613),
    ]
    r = _client(FakeStore(events), tmp_path, inbox_client).get("/api/audit")
    assert r.status_code == 200
    body = r.json()
    ids = [e["id"] for e in body["events"]]
    assert ids == [2, 1]
    assert body["events"][0]["event"] == "approve"
    assert body["events"][0]["actor_type"] == "human"


def test_audit_list_filters_honored(tmp_path, inbox_client):
    events = [
        _ev(1, "dispatch.start", actor_type="policy", issue_num=100),
        _ev(2, "approve", actor_type="human", issue_num=613),
        _ev(3, "dispatch.start", actor_type="policy", issue_num=613),
    ]
    c = _client(FakeStore(events), tmp_path, inbox_client)

    by_event = c.get("/api/audit?event=approve").json()["events"]
    assert [e["id"] for e in by_event] == [2]

    by_actor = c.get("/api/audit?actor_type=policy").json()["events"]
    assert [e["id"] for e in by_actor] == [3, 1]

    by_issue = c.get("/api/audit?issue=613").json()["events"]
    assert [e["id"] for e in by_issue] == [3, 2]


def test_audit_list_limit_clamped_and_cursor(tmp_path, inbox_client):
    events = [_ev(i, "e") for i in range(1, 6)]  # ids 1..5
    c = _client(FakeStore(events), tmp_path, inbox_client)

    # limit above 200 is rejected by the query validator.
    assert c.get("/api/audit?limit=500").status_code == 422

    page1 = c.get("/api/audit?limit=2").json()
    assert [e["id"] for e in page1["events"]] == [5, 4]
    assert page1["next_after"] == 4

    page2 = c.get(f"/api/audit?limit=2&after={page1['next_after']}").json()
    assert [e["id"] for e in page2["events"]] == [3, 2]
    assert page2["next_after"] == 2

    page3 = c.get(f"/api/audit?limit=2&after={page2['next_after']}").json()
    assert [e["id"] for e in page3["events"]] == [1]
    assert page3["next_after"] is None


def test_audit_list_payload_summary(tmp_path, inbox_client):
    events = [_ev(1, "approve", payload={"decision": "approved", "by": "op"})]
    body = _client(FakeStore(events), tmp_path, inbox_client).get("/api/audit").json()
    assert "decision" in (body["events"][0]["summary"] or "")


def test_audit_status_verifies_chain_without_anchors(tmp_path, inbox_client):
    body = _client(FakeStore(chain=_chain()), tmp_path, inbox_client).get(
        "/api/audit/status"
    )
    assert body.status_code == 200
    j = body.json()
    assert j["events"] == 2
    assert j["verified"] is True
    assert j["anchors_total"] == 0
    assert j["anchors_matched"] == 0


def test_audit_status_matches_anchors(tmp_path, inbox_client):
    rows = _chain()
    head = entry_hash(rows[-1])
    anchor = SimpleNamespace(
        id=2, entry_hash=head, count=2, timestamp=rows[-1].timestamp
    )
    (tmp_path / "audit-anchors.jsonl").write_text(format_anchor(anchor) + "\n")
    j = (
        _client(FakeStore(chain=rows), tmp_path, inbox_client)
        .get("/api/audit/status")
        .json()
    )
    assert j["verified"] is True
    assert j["anchors_total"] == 1
    assert j["anchors_matched"] == 1


def test_audit_status_detects_tamper(tmp_path, inbox_client):
    rows = _chain()
    tampered = rows[:1] + [
        ChainRow(**{**rows[1].__dict__, "prev_hash": "0" * 64})  # broken link
    ]
    j = (
        _client(FakeStore(chain=tampered), tmp_path, inbox_client)
        .get("/api/audit/status")
        .json()
    )
    assert j["verified"] is False


def test_audit_reads_require_session_under_expose(tmp_path, inbox_client):
    """Under --expose, unauthenticated reads must 401 — even from loopback."""

    @contextmanager
    def factory():
        yield FakeStore()

    app = create_app(store_factory=factory, repo_dir=tmp_path, expose=True)
    c = inbox_client(app, loopback_peer=True)
    assert c.get("/api/audit").status_code == 401
    assert c.get("/api/audit/status").status_code == 401
    assert (
        c.get(
            "/api/audit/histogram?since=2026-09-24T12:00:00Z&until=2026-09-24T13:00:00Z&bucket_s=600"
        ).status_code
        == 401
    )


def test_audit_list_pushes_limit_into_store(tmp_path, inbox_client):
    """list_audit uses limit+1 over-fetch in the store, not post-filtering."""
    events = [_ev(i, "e") for i in range(1, 11)]  # ids 1..10
    store = FakeStore(events)
    c = _client(store, tmp_path, inbox_client)

    r = c.get("/api/audit?limit=4").json()
    assert len(r["events"]) == 4
    assert r["next_after"] is not None
    # Store must have been called with limit+1 (5), never limit=None.
    assert store.last_list_limit == 5


def test_audit_status_caches_verify_on_unchanged_chain(tmp_path, inbox_client):
    """Second status call on an unchanged chain must not re-read chain rows."""
    store = FakeStore(chain=_chain())
    c = _client(store, tmp_path, inbox_client)

    r1 = c.get("/api/audit/status").json()
    reads_after_first = store.chain_reads

    r2 = c.get("/api/audit/status").json()
    # No additional chain reads on the second call.
    assert store.chain_reads == reads_after_first
    assert r1 == r2


def test_audit_status_cache_invalidated_on_chain_append(tmp_path, inbox_client):
    """Appending a row changes the cache key and triggers a fresh verify."""
    rows = _chain()
    store = FakeStore(chain=rows)
    c = _client(store, tmp_path, inbox_client)

    c.get("/api/audit/status")  # warm the cache
    reads_after_warm = store.chain_reads

    # Append a third row to the chain.
    r3 = ChainRow(
        id=3,
        timestamp="2026-07-15T00:02:00Z",
        event="approve",
        run_id="r3",
        issue_num=613,
        repo="kew",
        actor_type="human",
        actor_id="op",
        endpoint=None,
        policy_hash=None,
        payload=None,
        prev_hash=entry_hash(rows[-1]),
    )
    store._chain = rows + [r3]

    c.get("/api/audit/status")  # must be a cache miss → fresh read
    assert store.chain_reads > reads_after_warm


def test_audit_status_middle_row_tamper_detected_after_ttl(
    tmp_path, inbox_client, monkeypatch
):
    """A non-tail in-place edit is masked only within the cache TTL, then caught.

    The cache key is (count, tail_hash); editing a middle row changes neither,
    so a warm cache keeps reporting verified=True until the TTL lapses and the
    verdict is recomputed from a full chain read.
    """
    r1, r2 = _chain()
    r3 = ChainRow(
        id=3,
        timestamp="2026-07-15T00:02:00Z",
        event="approve",
        run_id="r3",
        issue_num=613,
        repo="kew",
        actor_type="human",
        actor_id="op",
        endpoint=None,
        policy_hash=None,
        payload=None,
        prev_hash=entry_hash(r2),
    )
    rows = [r1, r2, r3]
    store = FakeStore(chain=rows)
    c = _client(store, tmp_path, inbox_client)

    clock = [1000.0]
    monkeypatch.setattr(audit_svc, "_now", lambda: clock[0])

    assert c.get("/api/audit/status").json()["verified"] is True  # warm cache

    # Tamper the middle row in place: breaks r2's link to r1 but leaves the tail
    # (r3) and the row count unchanged, so the cache key is identical.
    rows[0] = ChainRow(**{**r1.__dict__, "event": "TAMPERED"})

    # Within the TTL the stale verdict is (knowingly) still served.
    clock[0] += audit_svc._STATUS_CACHE_TTL_S / 2
    assert c.get("/api/audit/status").json()["verified"] is True

    # Past the TTL the chain is re-verified from a full read and tamper is caught.
    clock[0] += audit_svc._STATUS_CACHE_TTL_S
    assert c.get("/api/audit/status").json()["verified"] is False


# --- A3/A4: the window, the filters and the histogram -----------------------
#
# These exercise SQL, so they run against a real SqliteRunStore: the exclusive
# `until` bound, the OR-grouped `item`/`epic`/`event_prefix` filters and the
# histogram's bucket arithmetic are the behaviour under test, and a duck-typed
# fake would only restate the implementation.

_T0 = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)

# One long payload, so `summary` is truncated past `epic_ref` (A3's field rule).
_LONG_PAYLOAD = {"epic_ref": "ui-refine", "note": "x" * 400}

# One digit past MAX_ISSUE_NUM_DIGITS: a decimal string no `issue_num` can hold.
_WIDE_ID = "9" * (MAX_ISSUE_NUM_DIGITS + 1)


def _at(minutes: float) -> str:
    """ISO stamp *minutes* after the window start, as the store records it."""
    return (_T0 + timedelta(minutes=minutes)).isoformat()


def _q(minutes: float) -> str:
    """The same stamp, Z-suffixed — a bare `+` in a query string is a space."""
    return _at(minutes).replace("+00:00", "Z")


@pytest.fixture
def seeded_store():
    """Five events: two epics, both key columns, one epic-level event.

    Every stamp is on a 10-minute boundary — exactly the alignment a float
    bucket index gets wrong — and `ui-refine`'s snapshot names nodes under both
    an issue number and a durable item id.
    """
    # TestClient serves on its own thread; the store is read from there.
    with SqliteRunStore(":memory:", check_same_thread=False) as store:
        store.append_epic_tick(
            epic_ref="ui-refine",
            tick_ts=_at(0),
            snapshot={"nodes": [{"id": "2219"}, {"id": "SFD-232"}]},
            actions={},
            snapshot_hash="h-ui-refine",
        )
        store.append_epic_tick(
            epic_ref="other-epic",
            tick_ts=_at(0),
            snapshot={"nodes": [{"id": "9999"}]},
            actions={},
            snapshot_hash="h-other",
        )
        # A snapshot node id too wide to bind as a SQLite int64 — the epic read
        # must drop it from the issue_num arm, not 500 (#2247).
        store.append_epic_tick(
            epic_ref="wide-ids",
            tick_ts=_at(0),
            snapshot={"nodes": [{"id": _WIDE_ID}]},
            actions={},
            snapshot_hash="h-wide",
        )
        for ts, event, issue_num, item_id, payload in (
            (_at(0), "epic_node_status", 2219, None, {"epic_ref": "ui-refine"}),
            (_at(10), "dispatch_completed", None, "SFD-232", _LONG_PAYLOAD),
            (_at(20), "epic_tick", None, None, {"epic_ref": "ui-refine"}),
            (_at(30), "epic_node_status", 9999, None, {"epic_ref": "other-epic"}),
            (_at(40), "dispatch_started", 2219, None, None),
        ):
            store.append_audit_event(
                timestamp=ts,
                event=event,
                issue_num=issue_num,
                item_id=item_id,
                actor_type="host",
                actor_id="kew",
                payload=payload,
            )
        yield store


def _ids(client, query: str) -> list[int]:
    r = client.get(f"/api/audit?{query}")
    assert r.status_code == 200, r.text
    return [e["id"] for e in r.json()["events"]]


def test_since_and_until_bound_the_page_with_until_exclusive(
    tmp_path, inbox_client, seeded_store
):
    c = _client(seeded_store, tmp_path, inbox_client)

    # The event stamped exactly at `until` is OUT; the one at `since` is in.
    assert _ids(c, f"since={_q(0)}&until={_q(40)}") == [4, 3, 2, 1]
    assert _ids(c, f"since={_q(10)}&until={_q(30)}") == [3, 2]
    # Adjacent windows tile the log: neither drops nor repeats the boundary row.
    assert _ids(c, f"since={_q(40)}&until={_q(50)}") == [5]


def test_paging_inside_a_range_returns_every_event_exactly_once(
    tmp_path, inbox_client, seeded_store
):
    c = _client(seeded_store, tmp_path, inbox_client)
    window = f"since={_q(0)}&until={_q(40)}&limit=2"

    seen: list[int] = []
    after: int | None = None
    for _ in range(5):  # bounded: a cursor that never ends is the bug
        q = window if after is None else f"{window}&after={after}"
        body = c.get(f"/api/audit?{q}").json()
        seen.extend(e["id"] for e in body["events"])
        after = body["next_after"]
        if after is None:
            break

    assert after is None
    assert seen == [4, 3, 2, 1]  # every event in the window, once


def test_item_matches_the_item_id_column_or_a_numeric_issue(
    tmp_path, inbox_client, seeded_store
):
    c = _client(seeded_store, tmp_path, inbox_client)
    assert _ids(c, "item=SFD-232") == [2]
    assert _ids(c, "item=2219") == [5, 1]
    assert _ids(c, "item=SFD-999") == []


def test_epic_returns_its_nodes_and_its_epic_level_events_only(
    tmp_path, inbox_client, seeded_store
):
    """Node events (either key column) plus `$.epic_ref` events, and nothing else."""
    c = _client(seeded_store, tmp_path, inbox_client)

    # 5: node by issue_num, 3: epic-level (no node id at all), 2: node by
    # item_id, 1: node by issue_num. 4 belongs to the other epic.
    assert _ids(c, "epic=ui-refine") == [5, 3, 2, 1]
    assert _ids(c, "epic=other-epic") == [4]
    assert _ids(c, "epic=no-such-epic") == []


def test_an_or_grouped_filter_narrows_as_a_whole_against_another_filter(
    tmp_path, inbox_client, seeded_store
):
    """An OR-grouped filter AND-ed with a second one narrows every arm of the OR.

    The groups are AND-joined, and OR binds looser than AND: unparenthesized,
    `epic=ui-refine&event=epic_tick` would answer with every ui-refine node
    event as well, because only the group's last arm would see the `event=`.
    """
    c = _client(seeded_store, tmp_path, inbox_client)

    # 3 is the only ui-refine event that is an `epic_tick`; 5, 2 and 1 are its
    # other nodes' events and must not come back alongside it.
    assert _ids(c, "epic=ui-refine&event=epic_tick") == [3]
    assert _ids(c, "item=2219&event=dispatch_started") == [5]
    assert _ids(c, "event_prefix=epic_node&event_prefix=dispatch_s&issue=9999") == [4]

    # The histogram shares the WHERE builder, so it must narrow identically.
    hist = c.get(
        f"/api/audit/histogram?since={_q(0)}&until={_q(60)}"
        "&bucket_s=600&epic=ui-refine&event=epic_tick"
    )
    assert hist.status_code == 200, hist.text
    assert hist.json()["total"] == 1


def test_a_filter_value_too_wide_for_int64_reads_empty_rather_than_500(
    tmp_path, inbox_client, seeded_store
):
    """A numeric filter past the int64 bind limit is an item id, not a 500.

    `item=` comes straight off the query string and an epic's node ids come off
    its journaled snapshot, so neither is bounded before the read. Bound as an
    integer, a 19-digit value raises `OverflowError` out of sqlite3 (and a
    4300-digit one raises `ValueError` out of `int()`) — HTTP 500 for a value
    no `issue_num` column could ever hold (#2247).
    """
    c = _client(seeded_store, tmp_path, inbox_client)

    assert _ids(c, f"item={_WIDE_ID}") == []
    assert _ids(c, f"item={'9' * 5000}") == []
    assert _ids(c, "epic=wide-ids") == []  # its one node id is `_WIDE_ID`

    hist = c.get(
        f"/api/audit/histogram?since={_q(0)}&until={_q(60)}&bucket_s=600&epic=wide-ids"
    )
    assert hist.status_code == 200, hist.text
    assert hist.json()["total"] == 0


def test_two_event_prefixes_or_together(tmp_path, inbox_client, seeded_store):
    c = _client(seeded_store, tmp_path, inbox_client)
    assert _ids(c, "event_prefix=epic_node&event_prefix=dispatch_s") == [5, 4, 1]
    assert _ids(c, "event_prefix=epic_node") == [4, 1]


def test_epic_ref_survives_a_payload_longer_than_the_summary_cap(
    tmp_path, inbox_client, seeded_store
):
    """`summary` is capped at 200 chars; `epic_ref` is read before that cut."""
    c = _client(seeded_store, tmp_path, inbox_client)
    event = c.get("/api/audit?item=SFD-232").json()["events"][0]

    assert len(event["summary"]) == audit_svc._SUMMARY_MAX
    assert "ui-refine" not in event["summary"][40:]  # the cut landed past it
    assert event["epic_ref"] == "ui-refine"
    assert event["item_id"] == "SFD-232"


def test_histogram_buckets_sum_to_total_and_match_a_paged_read(
    tmp_path, inbox_client, seeded_store
):
    c = _client(seeded_store, tmp_path, inbox_client)
    window = f"since={_q(0)}&until={_q(60)}"

    r = c.get(f"/api/audit/histogram?{window}&bucket_s=600")
    assert r.status_code == 200, r.text
    body = r.json()

    # Dense: every bucket in the window is present, empty ones included.
    assert len(body["buckets"]) == 6
    assert body["buckets"][0]["start"] == _at(0)
    assert body["buckets"][5]["counts"] == {}
    assert sum(sum(b["counts"].values()) for b in body["buckets"]) == body["total"]
    assert body["total"] == len(
        c.get(f"/api/audit?{window}&limit=200").json()["events"]
    )

    # Boundary alignment: an event stamped exactly on a bucket edge belongs to
    # the bucket it opens, not the one below it (a bare float CAST gets this
    # wrong and leaves bucket 1 empty).
    assert body["buckets"][1]["counts"] == {"dispatch_completed": 1}
    assert body["buckets"][0]["counts"] == {"epic_node_status": 1}


def test_histogram_honors_the_same_filters_as_the_feed(
    tmp_path, inbox_client, seeded_store
):
    c = _client(seeded_store, tmp_path, inbox_client)
    window = f"since={_q(0)}&until={_q(60)}"

    for filters in ("epic=ui-refine", "item=2219", "event_prefix=epic_node"):
        hist = c.get(f"/api/audit/histogram?{window}&bucket_s=600&{filters}").json()
        page = c.get(f"/api/audit?{window}&limit=200&{filters}").json()
        assert hist["total"] == len(page["events"]), filters
        assert (
            sum(sum(b["counts"].values()) for b in hist["buckets"]) == hist["total"]
        ), filters


def test_histogram_refuses_more_than_500_buckets(tmp_path, inbox_client, seeded_store):
    c = _client(seeded_store, tmp_path, inbox_client)
    window = f"since={_q(0)}&until={_q(60)}"

    # 3600 one-second buckets over the hour.
    assert c.get(f"/api/audit/histogram?{window}&bucket_s=1").status_code == 422
    # 360 is under the cap and answers.
    assert c.get(f"/api/audit/histogram?{window}&bucket_s=10").status_code == 200
    # Exactly at the cap.
    at_cap = f"since={_q(0)}&until={_q(500)}&bucket_s=60"
    assert c.get(f"/api/audit/histogram?{at_cap}").status_code == 200
    one_over = f"since={_q(0)}&until={_q(501)}&bucket_s=60"
    assert c.get(f"/api/audit/histogram?{one_over}").status_code == 422


def test_histogram_rejects_a_bucket_width_past_the_sqlite_bind_range(
    tmp_path, inbox_client, seeded_store
):
    """A width wider than int64 is a 422, not a 500 (#2247).

    The MAX_BUCKETS cap cannot catch it: a width that large splits any window
    into exactly one bucket, so `bucket_count` waves it through and the width
    then reaches `sqlite3` as a bound parameter, which raises `OverflowError`.
    Wider still (past float range) and the `span / bucket_s` division raises
    `OverflowError` itself, inside the one call the route only guards for
    `ValueError`. Both are HTTP 500 for a window that is simply not answerable.
    """
    c = _client(seeded_store, tmp_path, inbox_client)
    window = f"since={_q(0)}&until={_q(60)}"

    # The widest width that binds still answers — one bucket over the window.
    at_limit = c.get(f"/api/audit/histogram?{window}&bucket_s={SQLITE_INT_MAX}")
    assert at_limit.status_code == 200, at_limit.text
    assert len(at_limit.json()["buckets"]) == 1

    for width in (SQLITE_INT_MAX + 1, 10**30, 10**400, "9" * 5000):
        r = c.get(f"/api/audit/histogram?{window}&bucket_s={width}")
        assert r.status_code == 422, f"bucket_s={str(width)[:24]}… -> {r.status_code}"


@pytest.mark.parametrize("param", ["issue", "after"])
def test_an_integer_filter_past_the_sqlite_bind_range_is_a_422_not_a_500(
    param, tmp_path, inbox_client, seeded_store
):
    """`issue=`/`after=` bind as integers too, so they need the same range.

    Unbounded, a 19-digit value raises `OverflowError` out of the sqlite3 bind
    — a 500 off a query string anyone on loopback can type (#2247).
    """
    c = _client(seeded_store, tmp_path, inbox_client)
    window = f"since={_q(0)}&until={_q(60)}&bucket_s=600"

    assert c.get(f"/api/audit?{param}={SQLITE_INT_MAX}").status_code == 200
    for value in (SQLITE_INT_MAX + 1, -SQLITE_INT_MAX - 2, 10**400):
        assert c.get(f"/api/audit?{param}={value}").status_code == 422, value

    # The histogram takes `issue=` as well, through the same WHERE builder.
    if param == "issue":
        assert c.get(f"/api/audit/histogram?{window}&issue={10**30}").status_code == 422


def test_bucket_count_refuses_a_width_past_the_sqlite_bind_range():
    """The service function is total on its own, not just behind the route."""
    since, until = _T0, _T0 + timedelta(hours=1)

    assert audit_svc.bucket_count(since, until, SQLITE_INT_MAX) == 1
    for width in (SQLITE_INT_MAX + 1, 10**400):
        with pytest.raises(ValueError, match="at most"):
            audit_svc.bucket_count(since, until, width)


def test_histogram_rejects_a_backwards_or_empty_window(
    tmp_path, inbox_client, seeded_store
):
    c = _client(seeded_store, tmp_path, inbox_client)
    assert (
        c.get(
            f"/api/audit/histogram?since={_q(60)}&until={_q(0)}&bucket_s=600"
        ).status_code
        == 422
    )
    assert (
        c.get(
            f"/api/audit/histogram?since={_q(0)}&until={_q(0)}&bucket_s=600"
        ).status_code
        == 422
    )
    assert (
        c.get(
            f"/api/audit/histogram?since={_q(0)}&until={_q(60)}&bucket_s=0"
        ).status_code
        == 422
    )


def test_histogram_puts_an_event_on_a_bucket_edge_in_the_bucket_it_opens(
    tmp_path, inbox_client
):
    """The float-boundary regression: hourly events, hourly buckets.

    ``(julianday(ts) - julianday(since)) * 86400`` lands at 3599.9999999 for an
    event exactly one hour after ``since``, so a bare CAST files it under
    bucket 0 and leaves bucket 1 empty. Rounding the offset first is what keeps
    one event per bucket here.
    """
    with SqliteRunStore(":memory:", check_same_thread=False) as store:
        for hour in range(3):
            store.append_audit_event(
                timestamp=_at(60 * hour),
                event="epic_tick",
                actor_type="host",
            )
        c = _client(store, tmp_path, inbox_client)
        body = c.get(
            f"/api/audit/histogram?since={_q(0)}&until={_q(180)}&bucket_s=3600"
        ).json()

    assert [b["counts"] for b in body["buckets"]] == [{"epic_tick": 1}] * 3
    assert body["total"] == 3
