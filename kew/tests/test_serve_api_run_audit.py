"""GET /api/runs/{id}/audit — the run page's forward-paged event feed (C3/#2300).

The audit feed `/api/audit` serves can only page BACKWARDS: it orders newest
first and `after=` is an upper-exclusive id bound. A run page reads the other
direction — it opens at the run's first event, pages forward through the rest,
and a live run resumes from the cursor it last read. So this endpoint is the
same table read oldest-first behind a strict lower-exclusive `after_seq`.

Every assertion here runs against a real :class:`SqliteRunStore`, because what
is being pinned is the SQL — the ordering, the cursor bound, the `run_id`
narrowing and the cap — and a duck-typed double would pin only the Python that
calls it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from kew.audit import Actor, emit
from kew.run_store import RunRecord, SqliteRunStore
from kew.serve.app import create_app
from kew.service.audit import MAX_RUN_AUDIT, run_audit_page

_ACTOR = Actor("policy", "driver")
_START = datetime(2026, 3, 1, tzinfo=UTC)

RUN_ID = "run-under-test"
OTHER_RUN_ID = "some-other-run"
EVENT_COUNT = 12


def _seed(store: SqliteRunStore) -> None:
    """One run with `EVENT_COUNT` events, interleaved with another run's."""
    for run_id in (RUN_ID, OTHER_RUN_ID):
        store.save(
            RunRecord(
                run_id=run_id,
                issue_num=77,
                item_id="77",
                branch=f"agent/{run_id}",
                status="success",
                exit_code=0,
                started_at=_START,
                finished_at=_START + timedelta(minutes=5),
            )
        )
    for index in range(EVENT_COUNT):
        emit(
            store,
            f"phase_{index % 3}",
            actor=_ACTOR,
            run_id=RUN_ID,
            issue_num=77,
            item_id="77",
            payload={"step": index},
            strict=True,
        )
        # Interleaved so a page that forgot its run filter would pick these up.
        emit(
            store,
            "other_run_event",
            actor=_ACTOR,
            run_id=OTHER_RUN_ID,
            issue_num=78,
            strict=True,
        )


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "run-audit.db"
    with SqliteRunStore(str(path)) as store:
        _seed(store)
    return path


@pytest.fixture
def store(db: Path) -> Any:
    with SqliteRunStore(str(db)) as opened:
        yield opened


@pytest.fixture
def client(db: Path, tmp_path: Path, inbox_client: Any) -> TestClient:
    app = create_app(lambda: SqliteRunStore(str(db)), repo_dir=tmp_path)
    return inbox_client(app, loopback_peer=True)


# --------------------------------------------------------------------------- #
# The service: ordering, the cursor, the cap
# --------------------------------------------------------------------------- #


def test_the_page_reads_oldest_first_and_only_this_run(store: Any) -> None:
    page = run_audit_page(store, RUN_ID)

    assert page.run_id == RUN_ID
    assert [event.payload["step"] for event in page.events] == list(range(EVENT_COUNT))
    assert [event.seq for event in page.events] == sorted(
        event.seq for event in page.events
    )
    assert {event.event for event in page.events} == {"phase_0", "phase_1", "phase_2"}


def test_the_cursor_walks_forward_without_repeating_or_skipping(store: Any) -> None:
    """Paging by `next_seq` visits every event exactly once, in order."""
    seen: list[int] = []
    cursor = 0
    for _ in range(EVENT_COUNT):  # a bound, so a stuck cursor fails rather than hangs
        page = run_audit_page(store, RUN_ID, after_seq=cursor, limit=5)
        if not page.events:
            break
        seen.extend(event.payload["step"] for event in page.events)
        assert page.next_seq == page.events[-1].seq
        cursor = page.next_seq

    assert seen == list(range(EVENT_COUNT))


def test_an_exhausted_cursor_returns_no_events_and_holds_its_place(store: Any) -> None:
    """The live tail's steady state: nothing new, same cursor, no error."""
    full = run_audit_page(store, RUN_ID)
    tail = run_audit_page(store, RUN_ID, after_seq=full.next_seq)

    assert tail.events == []
    assert tail.next_seq == full.next_seq


def test_an_empty_first_page_reports_the_cursor_it_was_asked_with(store: Any) -> None:
    page = run_audit_page(store, "never-dispatched", after_seq=0)

    assert page.run_id == "never-dispatched"
    assert page.events == []
    assert page.next_seq == 0


def test_the_page_carries_the_payload_and_the_envelope_fields(store: Any) -> None:
    first = run_audit_page(store, RUN_ID, limit=1).events[0]

    assert first.event == "phase_0"
    assert first.actor_type == "policy"
    assert first.item_id == "77"
    assert first.payload == {"step": 0}
    assert first.ts.endswith("+00:00")


def test_an_out_of_range_limit_is_refused_not_quietly_clamped(store: Any) -> None:
    with pytest.raises(ValueError, match=f"between 1 and {MAX_RUN_AUDIT}"):
        run_audit_page(store, RUN_ID, limit=MAX_RUN_AUDIT + 1)
    with pytest.raises(ValueError, match=f"between 1 and {MAX_RUN_AUDIT}"):
        run_audit_page(store, RUN_ID, limit=0)


def test_the_run_filter_and_the_window_are_sql_predicates(store: Any) -> None:
    """The cost of a page is the page — never the ledger (the C3 rule)."""
    statements: list[str] = []
    store._committed_connection().set_trace_callback(statements.append)
    store._conn.set_trace_callback(statements.append)
    try:
        run_audit_page(store, RUN_ID, limit=4)
    finally:
        store._committed_connection().set_trace_callback(None)
        store._conn.set_trace_callback(None)

    # sqlite3's trace callback expands bound parameters, so the recorded SQL
    # carries the values rather than the `?` placeholders.
    reads = [sql for sql in statements if "FROM audit_events" in sql]
    assert len(reads) == 1, statements
    assert f"run_id = '{RUN_ID}'" in reads[0]
    assert "id > 0" in reads[0]
    assert "ORDER BY id ASC" in reads[0]
    assert "LIMIT 4" in reads[0]


# --------------------------------------------------------------------------- #
# The endpoint: shape, bounds, and the 404
# --------------------------------------------------------------------------- #


def test_the_endpoint_serves_the_documented_shape(client: TestClient) -> None:
    response = client.get(f"/api/runs/{RUN_ID}/audit", params={"limit": 3})
    assert response.status_code == 200
    body = response.json()

    assert body["run_id"] == RUN_ID
    assert len(body["events"]) == 3
    assert set(body["events"][0]) == {
        "seq",
        "ts",
        "event",
        "actor_type",
        "item_id",
        "payload",
    }
    assert body["next_seq"] == body["events"][-1]["seq"]


def test_the_endpoint_pages_forward_from_next_seq(client: TestClient) -> None:
    first = client.get(f"/api/runs/{RUN_ID}/audit", params={"limit": 5}).json()
    second = client.get(
        f"/api/runs/{RUN_ID}/audit",
        params={"limit": 5, "after_seq": first["next_seq"]},
    ).json()

    assert [event["payload"]["step"] for event in first["events"]] == [0, 1, 2, 3, 4]
    assert [event["payload"]["step"] for event in second["events"]] == [5, 6, 7, 8, 9]
    assert second["next_seq"] > first["next_seq"]


def test_the_endpoint_refuses_an_unbounded_or_negative_page(client: TestClient) -> None:
    base = f"/api/runs/{RUN_ID}/audit"
    assert client.get(base, params={"limit": 0}).status_code == 422
    assert client.get(base, params={"limit": MAX_RUN_AUDIT + 1}).status_code == 422
    assert client.get(base, params={"after_seq": -1}).status_code == 422
    assert client.get(base, params={"limit": MAX_RUN_AUDIT}).status_code == 200


def test_an_unknown_run_is_a_404_not_an_empty_page(client: TestClient) -> None:
    """ "No such run" and "that run recorded nothing" are different answers."""
    assert client.get("/api/runs/no-such-run/audit").status_code == 404


def test_a_poll_of_the_endpoint_reads_the_ledger_exactly_once(
    db: Path, tmp_path: Path, inbox_client: Any
) -> None:
    """One poll = one run-row read + one ledger page, and nothing else.

    The run page's live tail polls this route about once a second, so every
    read the handler makes it does not use is paid for per beat. The 404 check
    is therefore a point read of the run row, not the ``get_run`` projection —
    which would add the posture read, the stage read and the two verdict reads
    the detail path documents as its own, all computed and discarded here. The
    service-level assertion above cannot see any of that: it calls
    ``run_audit_page`` directly, below the route that opens the store.
    """
    statements: list[str] = []

    def traced_store() -> SqliteRunStore:
        store = SqliteRunStore(str(db))
        store._conn.set_trace_callback(statements.append)
        store._committed_connection().set_trace_callback(statements.append)
        return store

    client = inbox_client(
        create_app(traced_store, repo_dir=tmp_path), loopback_peer=True
    )
    response = client.get(f"/api/runs/{RUN_ID}/audit", params={"limit": 4})
    assert response.status_code == 200

    ledger_reads = [sql for sql in statements if "FROM audit_events" in sql]
    assert len(ledger_reads) == 1, statements
    assert f"run_id = '{RUN_ID}'" in ledger_reads[0]
    row_reads = [sql for sql in statements if "FROM runs" in sql]
    assert len(row_reads) == 1, statements
