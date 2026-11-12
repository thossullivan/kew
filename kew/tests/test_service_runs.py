"""Tests for the kew.service single read surface."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace
from typing import Any

import pytest

from kew.audit import Actor
from kew.evidence import EvidenceKind, Producer, ProducerKind, record_evidence
from kew.flight.evidence import AVAILABLE, UNAVAILABLE
from kew.run_store import SqliteRunStore
from kew.sensitive_prompt import (
    PROMPT_ARTIFACT_KIND,
    RETENTION_DAYS,
    default_root,
    purge_expired_prompts,
    retain_prompt,
)
from kew.service.models import MetaDTO, RunDTO, TimelineDTO
from kew.service.runs import (
    RUN_STAGES,
    build_meta,
    event_file_for,
    get_diff,
    get_flight_timeline,
    get_run,
    get_run_posture,
    get_run_prompt,
    get_run_work_view,
    get_timeline,
    list_flight_events,
    list_run_postures,
    list_runs,
    protected_prompt_root,
    run_exists,
    select_run_detail,
)
from kew.service.work import (
    RemoteObservation,
    TrackerItemFact,
    pending_run_max_age_s,
)


class FakeStore:
    def __init__(self, records):
        self._records = records

    def list_all(self):
        return list(self._records)


class FakeRecord:
    def __init__(self, run_id, log_file, model="claude-opus-4-8"):
        self.run_id = run_id
        self.issue_num = 312
        self.title = "rate-limit headers"
        self.runner_name = "claude-code"
        self.model = model
        self.status = "success"
        self.estimated_cost_usd = 0.006
        self.started_at = None
        self.finished_at = None
        self.log_file = log_file  # event file is DERIVED from this
        # Flight-recorder columns (F3a) — recorded on the store row.
        self.duration_seconds = 42.5
        self.input_tokens = 1200
        self.output_tokens = 340
        self.cache_read_tokens = 900
        self.cache_creation_tokens = 60
        self.diff_additions = 17
        self.diff_deletions = 4
        self.diff_files = 3
        self.review_cycles = 2
        self.pr_url = "https://github.com/kew/kew/pull/1"
        self.pr_state = "open"
        self.attempt_number = 1
        self.error_category = ""
        self.complexity = "medium"
        self.branch = "agent/issue-312"


def _seed(tmp_path: Path) -> FakeStore:
    log = tmp_path / "r1.log"
    log.write_text("agent log\n")
    events = tmp_path / "r1.events.jsonl"  # derived sibling of r1.log
    events.write_text(
        "\n".join(
            json.dumps(x)
            for x in [
                {"ts": 1.0, "type": "started", "issue": 312},
                {"ts": 2.0, "type": "tool_use", "tool": "edit", "target": "a.py"},
                {"ts": 2.1, "type": "snapshot", "tree": "abc123", "after": "edit"},
                {"ts": 3.0, "type": "finished", "exit_code": 0},
            ]
        )
        + "\n"
    )
    return FakeStore([FakeRecord("r1", log)])


def test_event_file_is_derived_from_log_file(tmp_path: Path):
    store = _seed(tmp_path)
    ef = event_file_for(store, "r1")
    assert ef == tmp_path / "r1.events.jsonl"


def test_list_runs_maps_records(tmp_path: Path):
    store = _seed(tmp_path)
    runs = list_runs(store)
    assert len(runs) == 1
    assert isinstance(runs[0], RunDTO)
    assert runs[0].run_id == "r1" and runs[0].issue_num == 312
    assert runs[0].cost_usd == 0.006


def test_every_stage_the_projection_can_emit_is_in_the_vocabulary(
    tmp_path: Path,
) -> None:
    """The map, the publish refinement and RUN_STAGES cannot drift apart.

    ``api.ts`` declares the Stage column's words by citing RUN_STAGES, so a
    stage emitted from outside it would be a word no client knows.
    """
    log = tmp_path / "r1.log"
    log.write_text("agent log\n")
    kinds = [
        "dispatch",
        "adversarial_review",
        "review_remediation",
        "ci_fix",
        "resolve",
        "readiness_probe",
        "",
    ]
    statuses = ["pending", "success", "awaiting_publication"]
    records = []
    for index, (kind, status) in enumerate(
        (kind, status) for kind in kinds for status in statuses
    ):
        record = FakeRecord(f"r{index}", log)
        record.run_kind = kind
        record.status = status
        records.append(record)

    emitted = {run.stage for run in list_runs(FakeStore(records), limit=len(records))}

    assert emitted <= set(RUN_STAGES)
    # And every word is reachable, so none of them is decoration.
    assert emitted == set(RUN_STAGES)


def test_run_without_a_posture_event_reports_not_recorded(tmp_path: Path) -> None:
    run = get_run(_seed(tmp_path), "r1")

    assert run is not None
    assert {
        key: run.model_dump().get(key)
        for key in (
            "posture",
            "filesystem_boundary",
            "network_boundary",
            "credential_names",
            "command_guard",
            "admission",
            "reason",
        )
    } == {
        "posture": "not recorded",
        "filesystem_boundary": "not recorded",
        "network_boundary": "not recorded",
        "credential_names": [],
        "command_guard": "not recorded",
        "admission": "not recorded",
        "reason": None,
    }


def test_run_reads_the_recorded_posture_by_exact_run_id(tmp_path: Path) -> None:
    store = _seed(tmp_path)
    calls: list[dict[str, object]] = []
    payload = {
        "posture": "sandboxed",
        "filesystem_boundary": "container-mount",
        "network_boundary": "operator-managed-unverified",
        "credential_names": ["ANTHROPIC_API_KEY"],
        "command_guard": "loaded",
        "admission": "policy",
        "reason": None,
    }

    def read_posture(**kwargs: object) -> list[SimpleNamespace]:
        calls.append(kwargs)
        return [SimpleNamespace(run_id="r1", payload=payload)]

    store.list_audit_events = read_posture

    run = get_run(store, "r1")

    assert run is not None
    for key, value in payload.items():
        assert getattr(run, key) == value
    # The posture read is scoped by exact run id — the point of this test. The
    # detail read also takes the verdict reads (C3/#2300); they are a separate
    # question, asserted as separate calls. This double answers every read with
    # the posture payload, which carries no verdict word, so the run-scoped arm
    # finds nothing and the work item's standing verdict is read after it.
    posture_calls = [c for c in calls if c.get("event") == "sandbox_resolved"]
    assert posture_calls == [{"limit": 1, "event": "sandbox_resolved", "run_id": "r1"}]
    assert [c for c in calls if c.get("event") != "sandbox_resolved"] == [
        {"limit": 1, "event": "adversarial_review_verdict", "run_id": "r1"},
        {"limit": 1, "event": "adversarial_review_verdict", "item_or_issue": "312"},
    ]


def test_list_run_postures_reads_only_requested_runs() -> None:
    store = FakeStore([])
    calls: list[dict[str, object]] = []

    def read_posture(**kwargs: object) -> list[SimpleNamespace]:
        calls.append(kwargs)
        run_id = str(kwargs["run_id"])
        return [SimpleNamespace(run_id=run_id, payload={"posture": "sandboxed"})]

    store.list_audit_events = read_posture

    postures = list_run_postures(store, ["r2", "r1", "r2"])

    assert set(postures) == {"r1", "r2"}
    assert calls == [
        {"limit": 1, "event": "sandbox_resolved", "run_id": "r2"},
        {"limit": 1, "event": "sandbox_resolved", "run_id": "r1"},
    ]


@pytest.mark.parametrize(
    "basis", ["operator-asserted", "pending-sidecar", "sidecar-admission"]
)
def test_posture_readers_preserve_recorded_egress_basis(tmp_path, basis):
    from kew.service.runs import get_run_posture

    store = _seed(tmp_path)
    store.list_audit_events = lambda **k: [
        SimpleNamespace(
            run_id="r1",
            payload={"posture": "sandboxed", "egress_verification_basis": basis},
        )
    ]
    postures = [
        get_run_posture(store, "r1"),
        list_run_postures(store, ["r1"])["r1"],
        get_run(store, "r1"),
        list_runs(store)[0],
    ]
    for posture in postures:
        assert posture.model_dump().get("egress_verification_basis") == basis


def _record_sidecar_posture(store, attempt, *, admitted):
    store.append_audit_event(
        timestamp=datetime.now(UTC).isoformat(),
        event="sandbox_resolved",
        run_id="r1",
        payload={
            "attempt_number": attempt,
            "posture": "sandboxed",
            "network_boundary": "sidecar-certified" if admitted else "unavailable",
            "egress_verification_basis": (
                "sidecar-admission" if admitted else "pending-sidecar"
            ),
            "credential_names": [f"ATTEMPT_{attempt}_KEY"] if admitted else [],
            "reason": f"attempt {attempt}",
        },
    )


@pytest.mark.parametrize("interleaved", [False, True], ids=["stale", "interleaved"])
@pytest.mark.parametrize(
    "read",
    [
        lambda store: get_run_posture(store, "r1"),
        lambda store: list_run_postures(store, ["r1"])["r1"],
        lambda store: get_run(store, "r1"),
        lambda store: list_runs(store)[0],
    ],
    ids=["posture", "postures", "run", "runs"],
)
def test_posture_readers_follow_newer_attempt_evidence(
    tmp_path, monkeypatch, read, interleaved
):
    from kew.run_store import RunRecord

    with SqliteRunStore(":memory:") as store:
        record = RunRecord(
            run_id="r1",
            issue_num=1945,
            branch="agent/1945",
            status="pending",
            exit_code=-1,
            attempt_number=1,
            started_at=datetime.now(UTC),
        )
        store.save(record)
        _record_sidecar_posture(store, 1, admitted=True)
        read_events = store.list_audit_events
        calls = []

        def start_retry_then_read(**kwargs):
            calls.append(kwargs)
            if interleaved and len(calls) == 1:
                # The reader already holds attempt one's row.
                record.attempt_number = 2
                store.save(record)
                _record_sidecar_posture(store, 2, admitted=False)
            return read_events(**kwargs)

        if not interleaved:
            record.attempt_number = 2
            store.save(record)
            _record_sidecar_posture(store, 2, admitted=False)
        monkeypatch.setattr(store, "list_audit_events", start_retry_then_read)
        posture = read(store)

        assert posture.egress_verification_basis == "pending-sidecar"
        assert posture.network_boundary == "unavailable"
        assert posture.reason == "attempt 2"
        assert posture.credential_names == []
        # Only the posture reads are counted: the detail read also takes one
        # verdict read scoped by work item (C3/#2300), which is a different
        # question from "did the posture reader follow the newer attempt".
        posture_calls = [c for c in calls if c.get("event") == "sandbox_resolved"]
        assert posture_calls == [
            {"event": "sandbox_resolved", "run_id": "r1", "limit": 1}
        ] * (2 if interleaved else 1)
        assert store.get("r1").attempt_number == 2


def test_posture_fallback_read_follows_newer_attempt_evidence(tmp_path, monkeypatch):
    from kew.run_store import RunRecord

    with SqliteRunStore(":memory:") as store:
        record = RunRecord(
            run_id="r1",
            issue_num=1945,
            branch="agent/1945",
            status="pending",
            exit_code=-1,
            attempt_number=2,
            started_at=datetime.now(UTC),
        )
        store.save(record)
        _record_sidecar_posture(store, 2, admitted=True)
        _record_sidecar_posture(store, 1, admitted=True)
        read_events = store.list_audit_events
        calls = []

        def start_retry_before_fallback(**kwargs):
            calls.append(kwargs)
            if len(calls) == 2:
                record.attempt_number = 3
                store.save(record)
                _record_sidecar_posture(store, 3, admitted=False)
            return read_events(**kwargs)

        monkeypatch.setattr(store, "list_audit_events", start_retry_before_fallback)
        posture = get_run_posture(store, "r1")

        assert posture.egress_verification_basis == "pending-sidecar"
        assert posture.network_boundary == "unavailable"
        assert posture.reason == "attempt 3"
        assert posture.credential_names == []
        assert len(calls) == 3


def _seed_retry_posture(store, *, fallback):
    from kew.run_store import RunRecord

    record = RunRecord(
        run_id="r1",
        issue_num=1945,
        branch="agent/1945",
        status="pending",
        exit_code=-1,
        attempt_number=2,
        started_at=datetime.now(UTC),
    )
    store.save(record)
    _record_sidecar_posture(store, 2, admitted=True)
    if fallback:
        _record_sidecar_posture(store, 1, admitted=True)
    return record


@pytest.mark.parametrize("fallback", [False, True], ids=["fast", "fallback"])
@pytest.mark.parametrize(
    "read",
    [
        lambda store: get_run_posture(store, "r1"),
        lambda store: list_run_postures(store, ["r1"])["r1"],
        lambda store: get_run(store, "r1"),
        lambda store: list_runs(store)[0],
    ],
    ids=["posture", "postures", "run", "runs"],
)
def test_round8_posture_snapshot_survives_retry_after_event_read(
    tmp_path, monkeypatch, read, fallback
):
    from kew.service import runs

    with SqliteRunStore(
        str(tmp_path / "threaded-posture.db"), check_same_thread=False
    ) as store:
        record = _seed_retry_posture(store, fallback=fallback)
        materialized = Event()
        retry_committed = Event()
        read_events = runs._posture_events
        calls = []
        results = []
        errors = []

        def pause_after_events(*args, **kwargs):
            events = read_events(*args, **kwargs)
            calls.append(kwargs)
            if len(calls) == (2 if fallback else 1):
                materialized.set()
                assert retry_committed.wait(10), "writer did not commit the retry"
            return events

        def read_in_thread():
            try:
                results.append(read(store))
            except BaseException as exc:
                errors.append(exc)

        monkeypatch.setattr(runs, "_posture_events", pause_after_events)
        reader = Thread(target=read_in_thread)
        reader.start()
        try:
            assert materialized.wait(10), "reader did not materialize the events"
            # This thread commits both writes while the reader holds attempt two.
            record.attempt_number = 3
            store.save(record)
            _record_sidecar_posture(store, 3, admitted=False)
            assert not store._conn.in_transaction
        finally:
            retry_committed.set()
            reader.join(timeout=10)

        assert not reader.is_alive()
        assert not errors, errors
        assert len(results) == 1
        posture = results[0]
        _assert_attempt_two_certification(posture)
        assert len(calls) == (2 if fallback else 1)
        assert store.get("r1").attempt_number == 3


def _assert_attempt_two_certification(posture):
    assert posture.egress_verification_basis == "sidecar-admission"
    assert posture.network_boundary == "sidecar-certified"
    assert posture.reason == "attempt 2"
    assert posture.credential_names == ["ATTEMPT_2_KEY"]
    if isinstance(posture, RunDTO):
        assert posture.attempt_number == 2


@pytest.mark.parametrize("matching_in_page", [True, False], ids=["present", "evicted"])
def test_posture_fallback_limits_returned_events(
    tmp_path, monkeypatch, matching_in_page
):
    with SqliteRunStore(str(tmp_path / "bounded-posture.db")) as store:
        _seed_retry_posture(store, fallback=False)
        for _ in range(40):
            _record_sidecar_posture(store, 1, admitted=True)
        if matching_in_page:
            _record_sidecar_posture(store, 2, admitted=False)
        _record_sidecar_posture(store, 1, admitted=True)
        from kew.service import runs

        read_events = runs._posture_events
        returned_counts = []
        statements = []

        def count_events(*args, **kwargs):
            events = read_events(*args, **kwargs)
            returned_counts.append(len(events))
            return events

        monkeypatch.setattr(runs, "_posture_events", count_events)
        store._committed_connection().set_trace_callback(statements.append)
        posture = get_run_posture(store, "r1")
        store._committed_connection().set_trace_callback(None)

        assert returned_counts == [1, 32]
        event_queries = [sql for sql in statements if "FROM audit_events" in sql]
        assert len(event_queries) == 2
        assert "LIMIT 32" in event_queries[1]
        _assert_bounded_pending_posture(posture, matching_in_page)


def _assert_bounded_pending_posture(posture, matching_in_page):
    assert posture.egress_verification_basis == "pending-sidecar"
    assert posture.network_boundary == "unavailable"
    assert posture.credential_names == []
    assert posture.reason == (
        "attempt 2"
        if matching_in_page
        else "latest attempt has no recorded sandbox posture"
    )


@pytest.mark.parametrize("fallback", [False, True], ids=["fast", "fallback"])
def test_posture_read_stops_after_three_racing_passes(tmp_path, monkeypatch, fallback):
    from kew.service import runs

    with SqliteRunStore(":memory:") as store:
        record = _seed_retry_posture(store, fallback=fallback)
        read_events = runs._posture_events
        get_record = store.get
        row_attempts = []
        event_reads = []

        def observe_row(run_id):
            row = get_record(run_id)
            row_attempts.append(row.attempt_number)
            return row

        def retry_after_events(*args, **kwargs):
            events = read_events(*args, **kwargs)
            event_reads.append(kwargs.get("limit"))
            if (kwargs.get("limit") != 1) == fallback:
                record.attempt_number += 1
                store.save(record)
                _record_sidecar_posture(store, record.attempt_number, admitted=True)
                if fallback:
                    _record_sidecar_posture(store, 1, admitted=True)
            return events

        def forbidden_snapshot():
            pytest.fail("posture reads must not open a shared-connection snapshot")

        monkeypatch.setattr(store, "get", observe_row)
        monkeypatch.setattr(store, "read_snapshot", forbidden_snapshot)
        monkeypatch.setattr(runs, "_posture_events", retry_after_events)
        posture = get_run_posture(store, "r1")

        assert posture.posture == "pending"
        assert posture.reason == "posture read raced a retry"
        assert posture.egress_verification_basis == "pending-sidecar"
        assert posture.network_boundary == "unavailable"
        assert posture.credential_names == []
        assert row_attempts == [2, 3, 3, 4, 4, 5]
        assert event_reads == ([1, 32] * 3 if fallback else [1] * 3)
        assert get_record("r1").attempt_number == 5


def _seed_posture_page(store, *, stale):
    from kew.run_store import RunRecord

    started = datetime(2026, 1, 1, tzinfo=UTC)
    for index in range(1000):
        store.save(
            RunRecord(
                run_id=f"r{index}",
                issue_num=index,
                branch=f"agent/{index}",
                status="failure",
                exit_code=1,
                started_at=started + timedelta(seconds=index),
                attempt_number=2,
            )
        )
        if index < 950:
            continue
        for attempt in [2, 1] if stale else [1, 2]:
            store.append_audit_event(
                timestamp=started.isoformat(),
                event="sandbox_resolved",
                run_id=f"r{index}",
                payload={
                    "attempt_number": attempt,
                    "posture": "sandboxed",
                    "filesystem_boundary": "container-mount",
                    "network_boundary": "operator-managed-unverified",
                    "egress_verification_basis": "operator-asserted",
                    "credential_names": [f"ATTEMPT_{attempt}_KEY"],
                    "reason": f"attempt {attempt}",
                },
            )


@pytest.mark.parametrize("stale", [False, True], ids=["latest", "late-old-event"])
def test_posture_page_has_bounded_queries(tmp_path, stale):
    from kew.service.runs import get_run_posture

    with SqliteRunStore(str(tmp_path / "page.db")) as store:
        _seed_posture_page(store, stale=stale)
        expected = {
            f"r{index}": get_run_posture(store, f"r{index}").model_dump()
            for index in range(950, 1000)
        }
        statements = []
        store._committed_connection().set_trace_callback(statements.append)
        page = list_runs(store)
        store._committed_connection().set_trace_callback(None)

    # Each run adds an event query and a row verification; stale heads add one query.
    assert len(statements) <= (300 if stale else 250), statements
    assert not any("FROM runs ORDER BY" in sql for sql in statements)
    assert [row.run_id for row in page] == [f"r{i}" for i in range(999, 949, -1)]
    for row in page:
        assert {key: getattr(row, key) for key in expected[row.run_id]} == expected[
            row.run_id
        ]
        assert row.credential_names == ["ATTEMPT_2_KEY"]


@pytest.mark.parametrize("batch", [False, True], ids=["single", "batch"])
def test_posture_lookup_uses_indexed_run_records(tmp_path, batch):
    from kew.service.runs import get_run_posture

    with SqliteRunStore(str(tmp_path / "lookup.db")) as store:
        _seed_posture_page(store, stale=False)
        statements = []
        store._committed_connection().set_trace_callback(statements.append)
        if batch:
            postures = list_run_postures(
                store, [f"r{i}" for i in range(950, 1000)] + ["r999"]
            )
        else:
            postures = {"r999": get_run_posture(store, "r999")}
        store._committed_connection().set_trace_callback(None)

    assert not any("FROM runs ORDER BY" in sql for sql in statements), statements
    count = 50 if batch else 1
    assert len(statements) == 5 * count
    assert sum("FROM runs WHERE run_id =" in sql for sql in statements) == 2 * count
    assert len(postures) == count
    assert all(
        posture.credential_names == ["ATTEMPT_2_KEY"] for posture in postures.values()
    )


def test_run_dto_round_trips_flight_recorder_columns(tmp_path: Path):
    store = _seed(tmp_path)
    run = list_runs(store)[0]
    _assert_recorder_usage(run)
    _assert_recorder_delivery(run)


def _assert_recorder_usage(run):
    assert run.duration_seconds == 42.5
    assert run.input_tokens == 1200
    assert run.output_tokens == 340
    assert run.cache_read_tokens == 900
    assert run.cache_creation_tokens == 60
    assert run.diff_additions == 17
    assert run.diff_deletions == 4
    assert run.diff_files == 3
    assert run.review_cycles == 2


def _assert_recorder_delivery(run):
    assert run.pr_url == "https://github.com/kew/kew/pull/1"
    assert run.pr_state == "open"
    assert run.attempt_number == 1
    assert run.error_category == ""
    assert run.complexity == "medium"
    assert run.branch == "agent/issue-312"


def test_run_dto_defaults_when_columns_absent(tmp_path: Path):
    # A record missing the flight-recorder attrs still maps (optional/defaulted).
    log = tmp_path / "r1.log"
    log.write_text("agent log\n")

    class Bare:
        run_id = "bare"
        issue_num = 1
        title = "t"
        runner_name = "claude-code"
        model = "claude-opus-4-8"
        status = "success"
        estimated_cost_usd = 0.0
        started_at = None
        finished_at = None
        log_file = log

    run = list_runs(FakeStore([Bare()]))[0]
    assert run.duration_seconds == 0.0
    assert run.input_tokens == 0
    assert run.diff_additions is None
    assert run.pr_url is None
    assert run.attempt_number == 1
    assert run.complexity == "" and run.branch == ""


def test_get_timeline_returns_the_activity_chain(tmp_path: Path):
    store = _seed(tmp_path)
    tl = get_timeline(store, "r1")
    assert isinstance(tl, TimelineDTO)
    assert [n.kind for n in tl.nodes] == ["init", "edit", "result"]
    # A step is an activity marker. The one diff there is belongs to the sealed
    # artifact (I16), so no node advertises one of its own.
    assert all(n.has_diff is False for n in tl.nodes)


def test_a_store_without_an_evidence_surface_reports_it_rather_than_raising(
    tmp_path: Path,
):
    # Older callers inject narrow RunStore-shaped fakes. A missing ledger is a
    # dimension that cannot be read, not an exception into a page render.
    store = _seed(tmp_path)

    flight = get_flight_timeline(store, "r1")

    assert flight.events == []
    assert flight.sealed is False
    assert flight.chain_verified is False
    assert {row.availability for row in flight.availability} == {"unavailable"}
    assert flight.runner_telemetry == "missing"


def test_a_diff_is_refused_when_nothing_is_sealed(tmp_path: Path):
    store = _seed(tmp_path)

    assert get_diff(store, "r1", "r1:1", tmp_path) is None


def test_unknown_run_returns_none(tmp_path: Path):
    store = _seed(tmp_path)
    assert get_timeline(store, "nope") is None
    assert event_file_for(store, "nope") is None


def test_run_exists_answers_only_the_404_question(tmp_path: Path):
    """The cheap existence read a polled route uses instead of ``get_run``.

    One point read of the run row and no projection: the posture, stage and
    verdict reads ``get_run`` makes are what a per-second poll of a run's
    ledger page would otherwise pay for and discard (C3/#2300).
    """
    store = _seed(tmp_path)

    assert run_exists(store, "r1") is True
    assert run_exists(store, "nope") is False


def test_hosted_model_run_has_hosted_channel(tmp_path: Path):
    store = _seed(tmp_path)
    assert list_runs(store)[0].channel == "hosted"


def test_local_model_run_has_local_channel(tmp_path: Path):
    log = tmp_path / "r1.log"
    log.write_text("agent log\n")
    store = FakeStore([FakeRecord("r1", log, model="ollama/qwen2.5-coder")])
    assert list_runs(store)[0].channel == "local"


def test_build_meta_reflects_billing_config():
    meta = build_meta(
        mode="subscription",
        plan_label="Claude Max",
        plan_monthly_usd=200.0,
        version="9.9.9",
    )
    assert isinstance(meta, MetaDTO)
    assert meta.billing == "subscription"
    assert meta.plan_label == "Claude Max"
    assert meta.plan_monthly_usd == 200.0
    assert meta.version == "9.9.9"


# --------------------------------------------------------------------------- #
# I17 — the journal slice and the protected prompt read
# --------------------------------------------------------------------------- #


_ACTOR = Actor("human", "tester")
_HOST = Producer(kind=ProducerKind.HOST, name="kew.dispatch", version="1.2.3")
_CREATED = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
_PROMPT = "SYSTEM: do the thing\nSECRET-PROMPT-BYTES-do-not-leak\n"


@pytest.fixture
def store(tmp_path: Path) -> Any:
    with SqliteRunStore(str(tmp_path / "dispatch.db")) as opened:
        yield opened


def _fact(store: Any, value: str, *, run_id: str = "run-1") -> int:
    return record_evidence(
        store,
        EvidenceKind.RECEIPT_FACT,
        actor=_ACTOR,
        producer=_HOST,
        run_id=run_id,
        facts={"fact": "outcome", "value": value},
    )


def _retain(store: Any, root: Path, *, run_id: str = "run-1", text: str = _PROMPT):
    """Retain real bytes under *root* and index them, as dispatch does."""
    ref = retain_prompt(
        default_root(root), run_id=run_id, attempt_number=1, text=text, now=_CREATED
    )
    store.record_artifact(
        run_id=run_id,
        attempt_number=1,
        kind=PROMPT_ARTIFACT_KIND,
        digest=ref.digest,
        byte_length=ref.byte_length,
        locator=ref.locator,
        created_at=_CREATED,
    )
    return ref


class TestTheJournalSliceIsCursored:
    def test_the_slice_starts_strictly_after_the_cursor(self, store: Any) -> None:
        first = _fact(store, "one")
        second = _fact(store, "two")
        third = _fact(store, "three")

        events = list_flight_events(store, "run-1", after_seq=first)

        assert [event.seq for event in events] == [second, third]

    def test_a_cursor_at_the_head_yields_nothing(self, store: Any) -> None:
        _fact(store, "one")
        head = _fact(store, "two")

        assert list_flight_events(store, "run-1", after_seq=head) == []

    def test_the_default_cursor_replays_the_whole_journal(self, store: Any) -> None:
        first = _fact(store, "one")
        second = _fact(store, "two")

        assert [e.seq for e in list_flight_events(store, "run-1")] == [first, second]

    def test_a_store_without_a_ledger_has_an_empty_journal(self, tmp_path: Path):
        assert list_flight_events(_seed(tmp_path), "r1") == []

    def test_the_journal_carries_no_reserved_content_keys(self, store: Any) -> None:
        _fact(store, "one")

        facts = [key for e in list_flight_events(store, "run-1") for key in e.facts]

        assert not set(facts) & {"prompt", "prompt_text", "compiled_prompt"}


class TestTheProtectedPromptRead:
    def test_it_returns_the_exact_retained_bytes(
        self, store: Any, tmp_path: Path
    ) -> None:
        ref = _retain(store, tmp_path)

        dto = get_run_prompt(store, "run-1", prompt_root=default_root(tmp_path))

        assert dto is not None
        assert dto.text == _PROMPT
        assert dto.digest == ref.digest
        assert dto.byte_length == ref.byte_length
        assert dto.availability == AVAILABLE
        assert dto.artifact_kind == PROMPT_ARTIFACT_KIND

    def test_a_run_that_retained_nothing_is_none_not_an_empty_reveal(
        self, store: Any, tmp_path: Path
    ) -> None:
        assert (
            get_run_prompt(store, "run-1", prompt_root=default_root(tmp_path)) is None
        )

    def test_a_store_without_an_artifact_index_reports_no_prompt(
        self, tmp_path: Path
    ) -> None:
        assert get_run_prompt(_seed(tmp_path), "r1", prompt_root=tmp_path) is None

    def test_a_purged_prompt_keeps_its_digest_and_provenance(
        self, store: Any, tmp_path: Path
    ) -> None:
        ref = _retain(store, tmp_path)
        store.set_artifact_retention(
            run_id="run-1",
            attempt_number=1,
            kind=PROMPT_ARTIFACT_KIND,
            terminal_at=_CREATED,
        )
        purge_expired_prompts(
            store,
            default_root(tmp_path),
            now=_CREATED + timedelta(days=RETENTION_DAYS + 1),
        )

        dto = get_run_prompt(store, "run-1", prompt_root=default_root(tmp_path))

        assert dto is not None
        assert dto.text is None
        assert dto.availability == UNAVAILABLE
        assert dto.digest == ref.digest
        assert dto.byte_length == ref.byte_length
        assert dto.purged_at is not None
        assert dto.purge_reason == "retention"
        assert dto.expires_at is not None

    def test_bytes_that_no_longer_match_the_digest_are_refused_not_shown(
        self, store: Any, tmp_path: Path
    ) -> None:
        ref = _retain(store, tmp_path)
        (default_root(tmp_path) / ref.locator).write_text("tampered", encoding="utf-8")

        dto = get_run_prompt(store, "run-1", prompt_root=default_root(tmp_path))

        assert dto is not None
        assert dto.text is None
        assert dto.availability == UNAVAILABLE
        assert "does not match" in dto.detail

    def test_an_indexed_prompt_whose_bytes_vanished_reports_unavailable(
        self, store: Any, tmp_path: Path
    ) -> None:
        ref = _retain(store, tmp_path)
        (default_root(tmp_path) / ref.locator).unlink()

        dto = get_run_prompt(store, "run-1", prompt_root=default_root(tmp_path))

        assert dto is not None
        assert dto.text is None
        assert dto.availability == UNAVAILABLE
        assert dto.detail


class TestTheProtectedRootIsWhereDispatchWroteIt:
    def test_it_is_anchored_on_the_configured_log_directory(
        self, tmp_path: Path
    ) -> None:
        logs = tmp_path / "logs"

        root = protected_prompt_root(
            SimpleNamespace(logging=SimpleNamespace(dir=str(logs))), tmp_path
        )

        assert root == default_root(logs.resolve())

    def test_it_falls_back_when_no_dispatch_config_is_wired(
        self, tmp_path: Path
    ) -> None:
        assert protected_prompt_root(None, tmp_path) == default_root(tmp_path)


# ===========================================================================
# I19 — the run surface asks the canonical projection, not RunDTO.status.
# ===========================================================================


def _admit_run(store: Any, **overrides: Any) -> None:
    from kew.run_store import AdmissionIdentity, RunRecord

    fields: dict[str, Any] = {
        "run_id": "r1",
        "issue_num": 42,
        "item_id": "42",
        "branch": "agent/issue-42",
        "status": "success",
        "exit_code": 0,
        "started_at": datetime(2026, 8, 18, 10, 0, tzinfo=UTC),
        "finished_at": datetime(2026, 8, 18, 10, 30, tzinfo=UTC),
        "runner_name": "claude-code",
        "model": "claude-opus-5",
        "attempt_number": 1,
    }
    fields.update(overrides)
    store.admit_run_with_identity(
        RunRecord(**fields),
        AdmissionIdentity(
            run_id=fields["run_id"],
            attempt_number=1,
            source_tracker="github",
            item_id=fields["item_id"],
            delivery_repo="acme/widgets",
        ),
    )


@pytest.fixture
def work_store(tmp_path: Path) -> Any:
    store = SqliteRunStore(str(tmp_path / "runs.db"))
    yield store
    store.close()


def test_run_work_view_reports_a_preserved_artifact_not_a_retry(
    work_store: Any,
) -> None:
    """``awaiting_publication`` is completed execution plus outstanding
    publication. The run surface must say "publish that artifact", because a
    redispatch here destroys a finished implementation."""
    _admit_run(work_store, status="awaiting_publication")

    view = get_run_work_view(work_store, "r1")

    assert view is not None
    assert view.execution.run_status == "awaiting_publication"
    assert view.execution.state == "completed"
    assert view.artifact.state == "preserved"
    assert view.exception.state == "awaiting_publication"
    assert view.safest_action.action == "finalize_publication"


def test_run_work_view_uses_the_persisted_pr_state_as_a_stale_reading(
    work_store: Any,
) -> None:
    """No network on a detail page. The last reconciliation is a real reading,
    marked stale — not a fresh truth and not a fabricated blank."""
    _admit_run(work_store, pr_url="http://pr/42", pr_state="OPEN")

    view = get_run_work_view(work_store, "r1")

    assert view is not None
    assert view.judgment.state == "awaiting_human_decision"
    assert view.judgment.observation.availability == "stale"
    assert view.judgment.observation.provenance == "cached"
    assert view.safest_action.action == "await_human_decision"


def test_run_work_view_without_a_persisted_pr_state_stays_unknown(
    work_store: Any,
) -> None:
    _admit_run(work_store, pr_url="http://pr/42")

    view = get_run_work_view(work_store, "r1")

    assert view is not None
    assert view.judgment.state == "unknown"
    assert view.exception.state == "remote_unavailable"
    assert view.safest_action.action == "observe_remote"


def test_run_work_view_is_none_for_an_unknown_run(work_store: Any) -> None:
    assert get_run_work_view(work_store, "nope") is None


def _raised_budget(minutes: int = 360) -> Any:
    """A config whose operator gave the agent *minutes* to finish."""
    from kew.config import AgentConfig, DispatchConfig

    config = DispatchConfig()
    config.agent = AgentConfig(timeout_minutes=minutes)
    return config


def test_run_work_view_reads_a_pending_row_against_the_bound_it_is_given(
    work_store: Any,
) -> None:
    """The run detail surface composes under the CALLER's in-flight bound.

    ``pending_run_max_age_s`` (#2279/D8) is twice the operator's
    ``agent.timeout_minutes``, floored at four hours, and ``view_revision``
    digests the composed view. A detail page that kept the bare floor while the
    projection beside it read a six-hour agent's budget printed
    ``run_abandoned`` over a run still going — and a revision no action on that
    item could ever match.
    """
    _admit_run(
        work_store,
        status="pending",
        started_at=datetime.now(UTC) - timedelta(hours=5),
        finished_at=None,
    )

    floored = get_run_work_view(work_store, "r1")
    bounded = get_run_work_view(
        work_store, "r1", pending_max_age_s=pending_run_max_age_s(_raised_budget())
    )

    assert floored is not None and bounded is not None
    # Both read the row as the same execution; only "is anybody waiting on a
    # person?" turns on the bound.
    assert floored.execution.state == bounded.execution.state == "in_flight"
    assert floored.exception.state == "run_abandoned"
    assert floored.safest_action.action == "inspect"
    assert bounded.exception.state == "in_flight"
    assert bounded.safest_action.action == "wait"
    assert floored.view_revision != bounded.view_revision


def test_select_run_detail_carries_the_bound_into_its_work_view(
    work_store: Any,
) -> None:
    """The bound rides the selection, not only the convenience wrapper: the TUI
    and ``kew status --run`` both read ``RunDetail.work``."""
    _admit_run(
        work_store,
        status="pending",
        started_at=datetime.now(UTC) - timedelta(hours=5),
        finished_at=None,
    )

    detail = select_run_detail(
        work_store, "r1", pending_max_age_s=pending_run_max_age_s(_raised_budget())
    )

    assert detail.work is not None
    assert detail.work.exception.state == "in_flight"


def test_run_work_view_and_run_dto_agree_on_the_same_run(work_store: Any) -> None:
    """The two reads are different questions about ONE row — the raw status
    stays verbatim in both, so a surface can show them side by side."""
    _admit_run(work_store, status="failure", error_category="timeout")

    run = get_run(work_store, "r1")
    view = get_run_work_view(
        work_store,
        "r1",
        tracker_item=TrackerItemFact(
            observation=RemoteObservation(
                dimension="tracker_item",
                availability="available",
                provenance="observed",
            ),
            state="open",
        ),
    )

    assert run is not None and view is not None
    assert run.status == view.execution.run_status == "failure"
    assert view.execution.error_category == run.error_category == "timeout"
    assert view.exception.state == "terminal_failure"


@pytest.mark.parametrize("journal", ["WAL", "DELETE"])
@pytest.mark.parametrize(
    "read",
    [
        lambda store: get_run_posture(store, "r1"),
        lambda store: list_run_postures(store, ["r1"])["r1"],
        lambda store: get_run(store, "r1"),
        lambda store: list_runs(store)[0],
    ],
    ids=["posture", "postures", "run", "runs"],
)
def test_round8_posture_reads_only_committed_events(tmp_path, journal, read):
    from concurrent.futures import ThreadPoolExecutor

    with SqliteRunStore(
        str(tmp_path / "committed.db"), check_same_thread=False
    ) as store:
        store._conn.execute(f"PRAGMA journal_mode={journal}")
        _seed_retry_posture(store, fallback=False)
        store._conn.execute("DELETE FROM audit_events")
        store._conn.commit()
        payload = json.dumps(
            {
                "attempt_number": 2,
                "posture": "sandboxed",
                "network_boundary": "sidecar-certified",
                "egress_verification_basis": "sidecar-admission",
            }
        )
        sql = "INSERT INTO audit_events(timestamp,event,run_id,payload) VALUES(?,?,?,?)"
        params = (datetime.now(UTC).isoformat(), "sandbox_resolved", "r1", payload)
        with ThreadPoolExecutor(max_workers=1) as executor:
            for commit in (False, True):
                store._conn.execute("BEGIN")
                store._conn.execute(sql, params)
                result = executor.submit(read, store).result(timeout=10)
                assert result.network_boundary != "sidecar-certified"
                assert store._conn.in_transaction
                if commit:
                    store._conn.commit()
                else:
                    store._conn.rollback()
                result = executor.submit(read, store).result(timeout=10)
                assert (result.network_boundary == "sidecar-certified") == commit
                if isinstance(result, RunDTO):
                    assert result.attempt_number == 2


@pytest.mark.parametrize("surface", ["get", "list"])
def test_round8_dto_uses_matched_attempt_row(tmp_path, monkeypatch, surface):
    from kew.service import runs

    with SqliteRunStore(str(tmp_path / "dto-race.db")) as store:
        record = _seed_retry_posture(store, fallback=False)
        record.title = "attempt 2"
        store.save(record)
        original = store.list_run_page if surface == "list" else runs._find_record
        raced = False

        def retry_after_row(*args, **kwargs):
            nonlocal raced
            selected = original(*args, **kwargs)
            if not raced:
                raced = True
                record.attempt_number = 3
                record.title = "attempt 3"
                store.save(record)
                _record_sidecar_posture(store, 3, admitted=True)
            return selected

        if surface == "list":
            monkeypatch.setattr(store, "list_run_page", retry_after_row)
            result = list_runs(store)[0]
        else:
            monkeypatch.setattr(runs, "_find_record", retry_after_row)
            result = get_run(store, "r1")
        assert raced
        assert result.reason == f"attempt {result.attempt_number}"
        assert result.title == result.reason
        assert result.attempt_number == (3 if surface == "list" else 2)
        assert result.network_boundary == "sidecar-certified"


def test_round8_memory_suppresses_open_transaction():
    with SqliteRunStore(":memory:") as store:
        _seed_retry_posture(store, fallback=False)
        store._conn.execute("BEGIN")
        store._conn.execute("UPDATE audit_events SET payload = payload")
        assert get_run_posture(store, "r1").network_boundary != "sidecar-certified"
        assert store._conn.in_transaction
        store._conn.rollback()
        assert get_run_posture(store, "r1").network_boundary == "sidecar-certified"


def test_round8_future_event_cannot_certify_older_row(tmp_path):
    with SqliteRunStore(str(tmp_path / "unmatched.db")) as store:
        _seed_retry_posture(store, fallback=False)
        _record_sidecar_posture(store, 3, admitted=True)
        for dto in (get_run(store, "r1"), list_runs(store)[0]):
            assert dto.attempt_number == 2
            assert dto.network_boundary != "sidecar-certified"
            assert dto.credential_names == []


def test_round8_memory_rejects_rollback_during_event_read(monkeypatch):
    with SqliteRunStore(":memory:") as store:
        _seed_retry_posture(store, fallback=False)
        store._conn.execute("DELETE FROM audit_events")
        store._conn.commit()
        original = store.list_audit_events

        def read_rolled_back_event(**filters):
            store._conn.execute(
                "INSERT INTO audit_events(timestamp,event,run_id,payload) "
                "VALUES(?,?,?,?)",
                (
                    datetime.now(UTC).isoformat(),
                    "sandbox_resolved",
                    "r1",
                    json.dumps(
                        {"attempt_number": 2, "network_boundary": "sidecar-certified"}
                    ),
                ),
            )
            events = original(**filters)
            store._conn.rollback()
            return events

        monkeypatch.setattr(store, "list_audit_events", read_rolled_back_event)
        assert get_run_posture(store, "r1").network_boundary != "sidecar-certified"
        assert not store._conn.in_transaction


@pytest.mark.parametrize("storage", ["file", "memory"])
def test_round10_run_work_view_pins_the_selected_record(tmp_path, monkeypatch, storage):
    from test_cli_status import _round10_retry, _round10_stores

    from kew.service import runs

    with _round10_stores(tmp_path, storage) as (store, writer):
        original = runs.get_work_view
        selected = []

        def retry_before_work(*args, **kwargs):
            _round10_retry(writer)
            view = original(*args, **kwargs)
            selected.append(view.identity.attempt_number)
            return view

        monkeypatch.setattr(runs, "get_work_view", retry_before_work)
        view = get_run_work_view(store, "r1")
        assert view is not None
        assert selected == [2]
        assert view.identity.attempt_number == 2
        assert view.execution.run_status == "awaiting_publication"
        assert view.artifact.state == "preserved"
        assert view.safest_action.action == "finalize_publication"


def test_round10_work_facts_use_the_committed_connection(tmp_path, monkeypatch):
    from test_cli_status import _round10_stores

    from kew.service import runs, work

    with _round10_stores(tmp_path, "file") as (store, writer):
        statements = []
        store._committed_connection().set_trace_callback(statements.append)
        original = runs.get_work_view
        collect = work.collect_work_facts

        def collect_committed(*args, **kwargs):
            facts = collect(*args, **kwargs)
            assert not facts.fleet_stopped
            return facts

        monkeypatch.setattr(work, "collect_work_facts", collect_committed)

        def change_fleet_before_work(*args, **kwargs):
            writer._conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
                ("fleet_stopped", json.dumps({"reason": "later stop"})),
            )
            writer._conn.commit()
            return original(*args, **kwargs)

        monkeypatch.setattr(runs, "get_work_view", change_fleet_before_work)
        view = get_run_work_view(store, "r1")
        assert view is not None
        assert view.safest_action.action == "finalize_publication"
        assert sum(sql == "BEGIN" for sql in statements) == 1
        assert sum(sql == "ROLLBACK" for sql in statements) == 1
        assert any("run_admission_identity" in sql for sql in statements)
        assert any("fleet_stopped" in sql for sql in statements)
        assert not store._conn.in_transaction


def test_round10_memory_work_read_does_not_touch_writer_transaction():
    with SqliteRunStore(":memory:") as store:
        _admit_run(store, attempt_number=2)
        _record_sidecar_posture(store, 2, admitted=True)
        store._conn.execute("BEGIN")
        view = get_run_work_view(store, "r1")
        assert view is not None
        assert view.identity.attempt_number == store.get("r1").attempt_number == 2
        posture = get_run_posture(store, "r1")
        assert posture.network_boundary != "sidecar-certified"
        assert posture.reason == "uncommitted posture unavailable"
        assert store._conn.in_transaction
        store._conn.rollback()


def test_round10_work_view_keeps_history_before_the_selected_run(work_store):
    for run_id, day in (("old", 1), ("selected", 2), ("newer", 3)):
        _admit_run(
            work_store,
            run_id=run_id,
            started_at=datetime(2026, 9, day, tzinfo=UTC),
        )
    view = get_run_work_view(work_store, "selected")
    assert view is not None
    assert view.identity.run_id == "selected"
    assert view.execution.attempts == 2
