"""The console's polls are bounded reads, not full-ledger reads (C3/#2296).

Every assertion here is about COST, on a store seeded with 2,000 runs: a
request's query count must not be a function of how much history the project
has. The console re-reads these endpoints every few seconds, so a read that
grows with the ledger means the console gets slower forever.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from kew.audit import Actor
from kew.evidence import EvidenceKind, Producer, ProducerKind, record_evidence
from kew.run_store import RunRecord, SqliteRunStore
from kew.serve.app import create_app
from kew.service import audit as audit_service
from kew.service.epics import get_epic_detail, list_epics
from kew.service.loops import list_loop_audit
from kew.service.runs import get_flight_timeline, list_runs

SEEDED_RUNS = 2000
EPICS = 70

_ACTOR = Actor("human", "tester")
_HOST = Producer(kind=ProducerKind.HOST, name="kew.dispatch", version="1.2.3")
_START = datetime(2026, 1, 1, tzinfo=UTC)


class _Counter:
    """Count every SQL statement a store executes, on both its connections.

    Reads take the read-only committed connection and the writer connection
    depending on the surface, so a count that watches only one of them can
    report a bounded read that is not one.
    """

    def __init__(self, store: SqliteRunStore) -> None:
        self._store = store
        self.statements: list[str] = []

    def _connections(self) -> list[Any]:
        found = [self._store._conn]
        with contextlib.suppress(Exception):
            found.append(self._store._committed_connection())
        return found

    def __enter__(self) -> _Counter:
        for connection in self._connections():
            connection.set_trace_callback(self.statements.append)
        return self

    def __exit__(self, *exc: object) -> None:
        for connection in self._connections():
            connection.set_trace_callback(None)

    def matching(self, needle: str) -> list[str]:
        return [sql for sql in self.statements if needle in sql]


def _seed_runs(store: SqliteRunStore, *, count: int = SEEDED_RUNS) -> None:
    for index in range(count):
        store.save(
            RunRecord(
                run_id=f"r{index:05d}",
                issue_num=index,
                item_id=str(index),
                branch=f"agent/{index}",
                status="success" if index % 2 else "pending",
                exit_code=0,
                started_at=_START + timedelta(seconds=index),
                finished_at=(_START + timedelta(seconds=index + 1))
                if index % 2
                else None,
                estimated_cost_usd=0.01,
            )
        )


@pytest.fixture
def seeded(tmp_path: Path) -> Any:
    with SqliteRunStore(str(tmp_path / "c3.db")) as store:
        _seed_runs(store)
        yield store


# --------------------------------------------------------------------------- #
# AC1 — /api/runs and /api/epics cost a bounded number of queries
# --------------------------------------------------------------------------- #


def test_runs_page_costs_a_bounded_number_of_queries(seeded: Any) -> None:
    """One page, one row query — never a read of all 2,000 runs."""
    with _Counter(seeded) as counted:
        page = list_runs(seeded, limit=50)

    assert [row.run_id for row in page] == [
        f"r{index:05d}" for index in range(SEEDED_RUNS - 1, SEEDED_RUNS - 51, -1)
    ]
    # The page itself is ONE query; the rest is the per-row posture/stage read,
    # which is bounded by the page size and not by the ledger.
    assert len(counted.matching("FROM runs ORDER BY")) == 0
    assert len(counted.matching("ORDER BY started_at DESC, rowid DESC")) == 1
    assert len(counted.statements) <= 6 * len(page) + 10, counted.statements


def test_runs_page_query_count_does_not_grow_with_the_ledger(tmp_path: Path) -> None:
    """The same page over 100 runs and over 2,000 costs the same."""
    counts = []
    for seeded_runs in (100, SEEDED_RUNS):
        with SqliteRunStore(str(tmp_path / f"grow-{seeded_runs}.db")) as store:
            _seed_runs(store, count=seeded_runs)
            with _Counter(store) as counted:
                list_runs(store, limit=50)
            counts.append(len(counted.statements))

    assert counts[0] == counts[1], counts


def test_runs_page_filters_and_pages_in_sql(seeded: Any) -> None:
    newest = list_runs(seeded, limit=2)
    following = list_runs(seeded, limit=2, before=newest[-1].started_at)

    assert [row.run_id for row in newest] == ["r01999", "r01998"]
    assert [row.run_id for row in following] == ["r01997", "r01996"]

    pending = list_runs(seeded, limit=5, status="pending")
    assert [row.status for row in pending] == ["pending"] * 5
    assert list_runs(seeded, limit=5, kind="adversarial_review") == []


def test_runs_endpoint_passes_the_window_through(
    tmp_path: Path, inbox_client: Any
) -> None:
    db = tmp_path / "endpoint.db"
    with SqliteRunStore(str(db)) as store:
        _seed_runs(store, count=100)
    app = create_app(lambda: SqliteRunStore(str(db)), repo_dir=tmp_path)
    client: TestClient = inbox_client(app, loopback_peer=True)

    page = client.get("/api/runs", params={"limit": 3, "status": "pending"})
    assert page.status_code == 200
    assert [row["run_id"] for row in page.json()] == ["r00098", "r00096", "r00094"]

    assert client.get("/api/runs", params={"limit": 0}).status_code == 422
    assert client.get("/api/runs", params={"limit": 10_000}).status_code == 422


def test_an_unbounded_page_is_refused_not_quietly_shortened(seeded: Any) -> None:
    """Both store shapes refuse the same out-of-range page."""

    class _NarrowStore:
        def list_all(self) -> list[Any]:
            return []

    for store in (seeded, _NarrowStore()):
        with pytest.raises(ValueError, match="between 1 and 200"):
            list_runs(store, limit=10_000)
        with pytest.raises(ValueError, match="between 1 and 200"):
            list_runs(store, limit=0)


def _seed_epics(store: SqliteRunStore, *, count: int = EPICS) -> None:
    for index in range(count):
        ref = f"epic-{index:03d}"
        for tick in range(3):
            store.append_epic_tick(
                epic_ref=ref,
                tick_ts=(_START + timedelta(minutes=tick)).isoformat(),
                snapshot={
                    "counts": {"done": tick},
                    "signal": "progressing",
                    "nodes": [{"id": str(index), "status": "in_progress"}],
                },
                actions={},
                snapshot_hash=f"{ref}-{tick}",
            )
        store.register_epic(ref)
        store.begin_epic_tick(ref, (_START + timedelta(minutes=3)).isoformat())
        store.finish_epic_tick(
            ref,
            tick_ts=(_START + timedelta(minutes=3)).isoformat(),
            duration_s=1.0,
            interval_s=20.0,
            signal="progressing",
            tick_id=None,
        )


def test_epics_roster_costs_a_bounded_number_of_queries(seeded: Any) -> None:
    """70 epics are read with a fixed handful of queries, not five each."""
    _seed_epics(seeded)

    with _Counter(seeded) as counted:
        roster = list_epics(seeded)

    assert len(roster) == EPICS
    # A constant: the roster read must not scale with the number of epics,
    # and certainly not with the 2,000 runs behind their drain sums.
    assert len(counted.statements) <= 20, len(counted.statements)
    assert len(counted.matching("FROM epic_ticks WHERE epic_ref")) == 0


def test_epics_roster_query_count_does_not_grow_with_the_roster(
    tmp_path: Path,
) -> None:
    counts = []
    for epics in (5, EPICS):
        with SqliteRunStore(str(tmp_path / f"roster-{epics}.db")) as store:
            _seed_runs(store, count=100)
            _seed_epics(store, count=epics)
            with _Counter(store) as counted:
                list_epics(store)
            counts.append(len(counted.statements))

    assert counts[0] == counts[1], counts


def test_batched_roster_facts_match_the_per_epic_reads(seeded: Any) -> None:
    """The batch must answer exactly what the five point reads answered."""
    _seed_epics(seeded, count=4)

    assert seeded.latest_epic_ticks().keys() == {f"epic-{i:03d}" for i in range(4)}
    for ref, tick in seeded.latest_epic_ticks().items():
        assert tick.snapshot_hash == seeded.latest_epic_tick(ref).snapshot_hash
    for ref, (count, first_ts) in seeded.epic_tick_spans().items():
        assert count == seeded.count_epic_ticks(ref)
        assert first_ts == seeded.first_epic_tick_ts(ref)
    for ref, state in seeded.list_epic_driver_states().items():
        assert state == seeded.get_epic_driver_state(ref)

    window = seeded.first_epic_tick_ts("epic-000")
    assert seeded.sum_costs_by_item_windows([("1", window), ("3", window)]) == {
        (item, window): cost
        for item, cost in seeded.sum_costs_by_item(["1", "3"], since=window).items()
    }


def test_windowed_drain_sums_are_keyed_by_window_not_by_item_alone(
    seeded: Any,
) -> None:
    """The same item under two windows is two answers, not one (#2375 review).

    Seeded run ``r00001`` for item ``1`` finished at 00:00:02, so a window
    opened after it must exclude it — and must not be able to overwrite the
    earlier window's answer just because both ask about item ``1``.
    """
    early = _START.isoformat()
    late = (_START + timedelta(seconds=30)).isoformat()

    totals = seeded.sum_costs_by_item_windows([("1", early), ("1", late)])

    assert totals == {("1", early): pytest.approx(0.01)}
    assert ("1", late) not in totals


def _seed_shared_node(store: SqliteRunStore) -> None:
    """One item, two runs, and two epics whose windows open either side of
    the first run — so the shared node's drain differs per epic."""
    for index, (offset, cost) in enumerate(((10, 0.02), (70, 0.05))):
        store.save(
            RunRecord(
                run_id=f"shared-{index}",
                issue_num=0,
                item_id="shared",
                branch=f"agent/shared-{index}",
                status="success",
                exit_code=0,
                started_at=_START + timedelta(seconds=offset - 1),
                finished_at=_START + timedelta(seconds=offset),
                estimated_cost_usd=cost,
            )
        )
    for ref, first_tick_offset in (("epic-early", 0), ("epic-late", 60)):
        store.append_epic_tick(
            epic_ref=ref,
            tick_ts=(_START + timedelta(seconds=first_tick_offset)).isoformat(),
            snapshot={
                "counts": {"in_progress": 1},
                "signal": "progressing",
                "nodes": [{"id": "shared", "status": "in_progress"}],
            },
            actions={},
            snapshot_hash=f"{ref}-0",
        )


def test_a_node_two_epics_share_drains_per_epic_window(tmp_path: Path) -> None:
    """A shared DAG node reports each epic's OWN window, not the last writer's.

    Keyed by node id alone, both roster rows took whichever epic was written
    last, and the Epics list then contradicted the epic detail page (#2375
    review). ``epic-early``'s window opens before both runs ($0.07);
    ``epic-late``'s opens between them ($0.05).
    """
    with SqliteRunStore(str(tmp_path / "shared.db")) as store:
        _seed_shared_node(store)
        roster = {row.epic_ref: row for row in list_epics(store)}

        assert roster["epic-early"].drain_total_usd == pytest.approx(0.07)
        assert roster["epic-late"].drain_total_usd == pytest.approx(0.05)
        # The per-epic detail read is the contract the roster must agree with.
        for ref, expected in (("epic-early", 0.07), ("epic-late", 0.05)):
            detail = get_epic_detail(store, ref)
            assert detail is not None
            assert detail.economics.node_drain_usd == {
                "shared": pytest.approx(expected)
            }


class _NoWindowBatch:
    """A store that predates ``sum_costs_by_item_windows`` — the fallback path.

    Everything else delegates, so the roster reads the same rows; only the
    batched windowed aggregate is unavailable.
    """

    def __init__(self, store: SqliteRunStore) -> None:
        self._store = store

    def __getattr__(self, name: str) -> Any:
        if name == "sum_costs_by_item_windows":
            raise AttributeError(name)
        return getattr(self._store, name)


def test_the_roster_drain_fallback_matches_the_batched_answer(tmp_path: Path) -> None:
    """Both drain paths answer per-epic-window, so neither can report a
    shared node as the other epic's spend (#2375 review)."""
    with SqliteRunStore(str(tmp_path / "fallback.db")) as store:
        _seed_shared_node(store)
        batched = {row.epic_ref: row.drain_total_usd for row in list_epics(store)}
        fallback = {
            row.epic_ref: row.drain_total_usd
            for row in list_epics(_NoWindowBatch(store))
        }

    assert fallback == batched
    assert batched["epic-early"] != batched["epic-late"]


def test_epic_stream_seeds_from_the_tail(seeded: Any) -> None:
    """A cold subscriber reads the newest ticks, not the oldest thousand."""
    from kew.service.epics import _MAX_SNAPSHOT_FRAMES, fetch_epic_frames

    for tick in range(_MAX_SNAPSHOT_FRAMES + 120):
        seeded.append_epic_tick(
            epic_ref="long",
            tick_ts=(_START + timedelta(minutes=tick)).isoformat(),
            snapshot={"counts": {}, "signal": "progressing", "nodes": []},
            actions={},
            snapshot_hash=f"long-{tick}",
        )
    newest_id = seeded.latest_epic_tick("long").id

    with _Counter(seeded) as counted:
        cursor, _frames = fetch_epic_frames(seeded, "long", {})

    tick_reads = [
        sql
        for sql in counted.matching("FROM epic_ticks")
        if f"ORDER BY id DESC LIMIT {_MAX_SNAPSHOT_FRAMES}" in sql
    ]
    assert len(tick_reads) == 1, counted.matching("FROM epic_ticks")
    assert not counted.matching("LIMIT 1000"), counted.statements
    # The cursor lands on the NEWEST row, so the subscriber never replays its
    # own history poll by poll before catching up.
    assert cursor["tick_id"] == newest_id


# --------------------------------------------------------------------------- #
# AC2 — /flight reuses the shared chain verdict
# --------------------------------------------------------------------------- #


def _seed_chain(store: Any, *, runs: int = 20) -> None:
    for index in range(runs):
        record_evidence(
            store,
            EvidenceKind.RECEIPT_FACT,
            actor=_ACTOR,
            producer=_HOST,
            run_id=f"r{index:05d}",
            facts={"fact": "outcome", "value": "ok"},
        )


@pytest.fixture(autouse=True)
def _cold_chain_cache() -> Any:
    audit_service._status_cache.clear()
    yield
    audit_service._status_cache.clear()


def test_flight_does_not_call_the_full_chain_verifier(
    seeded: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The verdict comes from the shared memo the status poll already filled."""
    _seed_chain(seeded)
    warm = audit_service.chain_verdict(seeded)
    assert warm is not None and warm.ok

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("/flight re-verified the whole chain")

    monkeypatch.setattr("kew.journal_replay.verify_chain", refuse)
    monkeypatch.setattr("kew.service.audit.verify_chain", refuse)

    timeline = get_flight_timeline(seeded, "r00003")

    assert timeline.chain_verified is True
    assert [event.facts["value"] for event in timeline.events] == ["ok"]


def test_flight_replays_one_run_in_sql(seeded: Any) -> None:
    """The run filter is a predicate, not a Python pass over every row."""
    _seed_chain(seeded)
    audit_service.chain_verdict(seeded)

    with _Counter(seeded) as counted:
        timeline = get_flight_timeline(seeded, "r00003")

    assert [event.facts["value"] for event in timeline.events] == ["ok"]
    evidence_reads = [
        sql for sql in counted.matching("FROM audit_events") if "event >=" in sql
    ]
    assert evidence_reads, counted.statements
    assert all("run_id = 'r00003'" in sql for sql in evidence_reads), evidence_reads
    assert not counted.matching("FROM chain_rows")


def test_the_chain_verdict_is_shared_and_still_bounded(seeded: Any) -> None:
    """One verdict per (ledger, tail) — and a new row invalidates it."""
    _seed_chain(seeded, runs=3)
    first = audit_service.chain_verdict(seeded)
    assert first is not None

    with _Counter(seeded) as counted:
        again = audit_service.chain_verdict(seeded)
    assert again is first
    assert not counted.matching("ORDER BY id ASC")

    _seed_chain(seeded, runs=1)
    assert audit_service.chain_verdict(seeded) is not first


def test_the_chain_verdict_is_none_without_a_ledger() -> None:
    """A store with no chain surface reports no verdict, never a clean one."""
    assert audit_service.chain_verdict(object()) is None


# --------------------------------------------------------------------------- #
# The loop audit page and the config parse
# --------------------------------------------------------------------------- #


def test_loop_audit_is_bounded_and_filtered_in_sql(seeded: Any) -> None:
    for index in range(40):
        seeded.append_audit_event(
            timestamp=(_START + timedelta(seconds=index)).isoformat(),
            event="iteration_recorded",
            payload={"loop_id": "L1" if index % 2 else "L2", "summary": f"i{index}"},
        )

    with _Counter(seeded) as counted:
        events = list_loop_audit(seeded, "L1", limit=5)

    assert [event.loop_id for event in events] == ["L1"] * 5
    reads = counted.matching("FROM audit_events")
    assert len(reads) == 1
    assert "json_extract(payload, '$.loop_id') = 'L1'" in reads[0]
    assert "LIMIT 5" in reads[0]


def test_kew_toml_is_parsed_once_per_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kew import config as config_module

    config_path = tmp_path / "kew.toml"
    config_path.write_text("[budget]\nper_issue_usd = 3.0\n", encoding="utf-8")
    config_module._toml_cache.clear()

    parses = []
    real = config_module._read_toml
    monkeypatch.setattr(
        config_module,
        "_read_toml",
        lambda path: (parses.append(path), real(path))[1],
    )

    first = config_module.load_config(config_path)
    second = config_module.load_config(config_path)

    assert first.budget.per_issue_usd == 3.0
    assert second.budget.per_issue_usd == 3.0
    assert len(parses) == 1

    config_path.write_text("[budget]\nper_issue_usd = 4.0\n", encoding="utf-8")
    assert config_module.load_config(config_path).budget.per_issue_usd == 4.0
    assert len(parses) == 2


def test_a_cached_parse_is_never_handed_out_to_be_mutated(
    tmp_path: Path,
) -> None:
    """load_config mutates the raw mapping; the cache must not accumulate it."""
    from kew import config as config_module

    config_path = tmp_path / "kew.toml"
    config_path.write_text("[budget]\nper_issue_usd = 3.0\n", encoding="utf-8")
    config_module._toml_cache.clear()

    first = config_module._read_toml_cached(config_path)
    first["budget"]["per_issue_usd"] = 99.0
    first["injected"] = True

    second = config_module._read_toml_cached(config_path)
    assert second == {"budget": {"per_issue_usd": 3.0}}
