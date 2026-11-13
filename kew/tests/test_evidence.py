"""The audit ledger is the one host evidence sequence (I06, Tasks 22-24).

There is no second evidence table, sequence, or chain: every fact these
writers record is an ``audit_events`` row, ordered by ``audit_events.id``,
carrying producer provenance and a schema version inside the already-hashed
payload. Required receipt facts fail closed; optional telemetry stays
best-effort and its absence is reported, never inferred as zero.
"""

from __future__ import annotations

import ast
import pathlib
from datetime import UTC, datetime
from typing import Any

import pytest

from kew.audit import Actor
from kew.audit_chain import verify_chain
from kew.evidence import (
    EVIDENCE_SCHEMA_VERSION,
    AttemptedChange,
    EvidenceFactError,
    EvidenceKind,
    EvidenceWriteError,
    Producer,
    ProducerKind,
    emit_evidence,
    import_runner_evidence,
    record_evidence,
)
from kew.journal_replay import replay_host_sequence, telemetry_status
from kew.run_store import SqliteRunStore

_ACTOR = Actor("human", "tester")
_HOST = Producer(kind=ProducerKind.HOST, name="kew.dispatch", version="1.2.3")
_RUNNER = Producer(kind=ProducerKind.RUNNER, name="claude-code", version="9.9")


@pytest.fixture
def store(tmp_path):
    with SqliteRunStore(str(tmp_path / "dispatch.db")) as opened:
        yield opened


def _receipt(store: Any, value: str = "ok", **kwargs: Any) -> int:
    return record_evidence(
        store,
        EvidenceKind.RECEIPT_FACT,
        actor=_ACTOR,
        producer=_HOST,
        run_id=kwargs.pop("run_id", "run-1"),
        facts={"fact": "outcome", "value": value},
        **kwargs,
    )


def _change(store: Any, subject: str, disposition: AttemptedChange) -> int:
    return record_evidence(
        store,
        EvidenceKind.ATTEMPTED_CHANGE,
        actor=_ACTOR,
        producer=_HOST,
        run_id="run-1",
        facts={"subject": subject, "disposition": disposition},
    )


# ---------------------------------------------------------------------------
# One sequence: audit_events.id, not the wall clock
# ---------------------------------------------------------------------------


def test_evidence_is_appended_to_the_existing_audit_chain(store) -> None:
    _receipt(store)
    rows = store.read_chain_rows()
    assert len(rows) == 1
    assert rows[0].event == EvidenceKind.RECEIPT_FACT.value
    assert verify_chain(rows).ok


def test_no_second_evidence_table_is_created(store) -> None:
    _receipt(store)
    names = {
        row[0]
        for row in store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    assert not [n for n in names if "evidence" in n], names


def test_the_sequence_is_row_id_order_even_when_clocks_disagree(store) -> None:
    _receipt(store, "first", now=datetime(2030, 1, 1, tzinfo=UTC))
    _receipt(store, "second", now=datetime(2000, 1, 1, tzinfo=UTC))
    _receipt(store, "third", now=datetime(2015, 1, 1, tzinfo=UTC))
    replayed = replay_host_sequence(store)
    assert [r.facts["value"] for r in replayed.records] == [
        "first",
        "second",
        "third",
    ]
    assert [r.audit_id for r in replayed.records] == sorted(
        r.audit_id for r in replayed.records
    )


def test_the_sequence_continues_across_a_restart(tmp_path) -> None:
    db = str(tmp_path / "dispatch.db")
    with SqliteRunStore(db) as first:
        _receipt(first, "before")
    with SqliteRunStore(db) as second:
        _receipt(second, "after")
        replayed = replay_host_sequence(second)
    assert [r.facts["value"] for r in replayed.records] == ["before", "after"]
    assert replayed.chain.ok


# ---------------------------------------------------------------------------
# Producer provenance
# ---------------------------------------------------------------------------


def test_every_row_names_its_producer_and_the_schema_it_was_written_under(
    store,
) -> None:
    _receipt(store)
    record = replay_host_sequence(store).records[0]
    assert record.producer == _HOST
    assert record.schema_version == EVIDENCE_SCHEMA_VERSION
    assert record.attested_by_host is True


def test_imported_runner_evidence_is_never_mistaken_for_a_host_attestation(
    store,
) -> None:
    import_runner_evidence(
        store,
        EvidenceKind.RECEIPT_FACT,
        actor=_ACTOR,
        producer=_RUNNER,
        run_id="run-1",
        facts={"fact": "outcome", "value": "ok"},
    )
    record = replay_host_sequence(store).records[0]
    assert record.producer.kind is ProducerKind.RUNNER
    assert record.attested_by_host is False


def test_a_producerless_row_is_replayed_as_an_explicit_gap(store) -> None:
    store.append_audit_event(
        timestamp=datetime.now(UTC).isoformat(),
        event=EvidenceKind.RECEIPT_FACT.value,
        run_id="run-1",
        payload={"evidence_schema": EVIDENCE_SCHEMA_VERSION, "facts": {}},
    )
    replayed = replay_host_sequence(store)
    assert replayed.records == ()
    assert [g.reason for g in replayed.gaps] == ["missing_provenance"]
    assert replayed.ok is False


def test_a_row_from_a_newer_evidence_schema_is_a_gap_not_a_guess(store) -> None:
    store.append_audit_event(
        timestamp=datetime.now(UTC).isoformat(),
        event=EvidenceKind.RECEIPT_FACT.value,
        run_id="run-1",
        payload={
            "evidence_schema": EVIDENCE_SCHEMA_VERSION + 1,
            "producer_kind": "host",
            "producer_name": "future",
            "producer_version": "2",
            "attested_by_host": True,
            "facts": {"fact": "outcome", "value": "ok"},
        },
    )
    replayed = replay_host_sequence(store)
    assert [g.reason for g in replayed.gaps] == ["unsupported_schema"]


# ---------------------------------------------------------------------------
# Required versus best-effort
# ---------------------------------------------------------------------------


def test_a_required_fact_that_cannot_be_written_fails_closed(store) -> None:
    class _Refusing:
        def append_audit_event(self, **_: Any) -> int | None:
            raise RuntimeError("disk full")

    with pytest.raises(EvidenceWriteError):
        _receipt(_Refusing())


def test_a_required_fact_that_writes_nothing_fails_closed() -> None:
    class _Silent:
        def append_audit_event(self, **_: Any) -> int | None:
            return None

    with pytest.raises(EvidenceWriteError):
        _receipt(_Silent())


def test_a_store_without_the_audit_api_cannot_receive_required_evidence() -> None:
    with pytest.raises(EvidenceWriteError):
        _receipt(object())


def test_a_missing_required_fact_is_refused_before_any_write(store) -> None:
    with pytest.raises(EvidenceFactError):
        record_evidence(
            store,
            EvidenceKind.RECEIPT_FACT,
            actor=_ACTOR,
            producer=_HOST,
            run_id="run-1",
            facts={"fact": "outcome"},
        )
    assert store.read_chain_rows() == []


def test_an_empty_required_fact_is_not_a_fact(store) -> None:
    with pytest.raises(EvidenceFactError):
        record_evidence(
            store,
            EvidenceKind.RECEIPT_FACT,
            actor=_ACTOR,
            producer=_HOST,
            run_id="run-1",
            facts={"fact": "outcome", "value": "   "},
        )


def test_optional_telemetry_stays_best_effort() -> None:
    class _Refusing:
        def append_audit_event(self, **_: Any) -> int | None:
            raise RuntimeError("disk full")

    assert (
        emit_evidence(
            _Refusing(),
            EvidenceKind.RUNNER_USAGE,
            actor=_ACTOR,
            producer=_RUNNER,
            run_id="run-1",
            facts={"metric": "tokens", "value": 12},
        )
        is None
    )


class _RefusingDedup:
    """A store whose dedup write path raises a raw store error."""

    def append_audit_event(self, **_: Any) -> int | None:
        raise AssertionError("the dedup path must use append_audit_event_once")

    def append_audit_event_once(self, **_: Any) -> int | None:
        raise RuntimeError("db locked")


def test_optional_telemetry_on_the_dedup_path_still_never_raises() -> None:
    # Regression: the dedup branch once bypassed the error wrapper, so a raw
    # store failure escaped emit_evidence's "never raises" contract.
    assert (
        emit_evidence(
            _RefusingDedup(),
            EvidenceKind.RUNNER_USAGE,
            actor=_ACTOR,
            producer=_RUNNER,
            run_id="run-1",
            facts={"metric": "tokens", "value": 12},
            dedup_key="dk-1",
        )
        is None
    )


def test_a_required_fact_on_the_dedup_path_fails_closed_as_evidence_error() -> None:
    # It still fails closed (raises), but as the documented EvidenceWriteError,
    # not the raw store exception a caller catching EvidenceWriteError would miss.
    with pytest.raises(EvidenceWriteError):
        _receipt(_RefusingDedup(), dedup_key="dk-2")


def test_best_effort_emit_refuses_to_downgrade_a_required_kind(store) -> None:
    with pytest.raises(EvidenceWriteError):
        emit_evidence(
            store,
            EvidenceKind.RECEIPT_FACT,
            actor=_ACTOR,
            producer=_HOST,
            run_id="run-1",
            facts={"fact": "outcome", "value": "ok"},
        )


def test_missing_telemetry_is_explicit_and_never_reads_as_zero(store) -> None:
    _receipt(store)
    emit_evidence(
        store,
        EvidenceKind.RUNNER_USAGE,
        actor=_ACTOR,
        producer=_RUNNER,
        run_id="run-1",
        facts={"metric": "tokens", "value": 12},
    )
    status = telemetry_status(replay_host_sequence(store))
    assert status[EvidenceKind.RUNNER_USAGE.value] == "present"
    assert status[EvidenceKind.RUNNER_PROGRESS.value] == "unavailable"


# ---------------------------------------------------------------------------
# Attempted-change checkpoints
# ---------------------------------------------------------------------------


def test_every_attempted_change_disposition_stays_distinguishable(store) -> None:
    for disposition in AttemptedChange:
        _change(store, f"file-{disposition.value}.py", disposition)
    replayed = replay_host_sequence(store)
    seen = {r.facts["subject"]: r.facts["disposition"] for r in replayed.records}
    assert seen == {f"file-{d.value}.py": d.value for d in AttemptedChange}
    assert len(set(seen.values())) == len(AttemptedChange)


def test_an_unknown_disposition_is_refused(store) -> None:
    with pytest.raises(EvidenceFactError):
        record_evidence(
            store,
            EvidenceKind.ATTEMPTED_CHANGE,
            actor=_ACTOR,
            producer=_HOST,
            run_id="run-1",
            facts={"subject": "a.py", "disposition": "maybe"},
        )


def test_an_unavailable_attempt_is_not_the_same_as_a_dropped_one(store) -> None:
    _change(store, "a.py", AttemptedChange.UNAVAILABLE)
    _change(store, "b.py", AttemptedChange.DROPPED)
    records = replay_host_sequence(store).records
    assert records[0].facts["disposition"] != records[1].facts["disposition"]


# ---------------------------------------------------------------------------
# Prompt bytes never enter an evidence payload
# ---------------------------------------------------------------------------


def test_an_unknown_fact_key_is_refused(store) -> None:
    with pytest.raises(EvidenceFactError):
        record_evidence(
            store,
            EvidenceKind.RECEIPT_FACT,
            actor=_ACTOR,
            producer=_HOST,
            run_id="run-1",
            facts={"fact": "outcome", "value": "ok", "prompt": "the exact bytes"},
        )


@pytest.mark.parametrize(
    "key", ["prompt", "prompt_text", "compiled_prompt", "transcript"]
)
def test_a_reserved_prompt_key_is_refused_on_every_kind(store, key: str) -> None:
    with pytest.raises(EvidenceFactError):
        record_evidence(
            store,
            EvidenceKind.ARTIFACT,
            actor=_ACTOR,
            producer=_HOST,
            run_id="run-1",
            facts={
                "artifact_kind": "compiled_prompt",
                "digest": "sha256:" + "0" * 64,
                "byte_length": 10,
                "storage": "local_protected",
                key: "the exact bytes",
            },
        )


def test_an_artifact_row_records_only_the_digest_and_its_metadata(store) -> None:
    record_evidence(
        store,
        EvidenceKind.ARTIFACT,
        actor=_ACTOR,
        producer=_HOST,
        run_id="run-1",
        facts={
            "artifact_kind": "compiled_prompt",
            "digest": "sha256:" + "a" * 64,
            "byte_length": 4096,
            "storage": "local_protected",
        },
    )
    payload = store.read_chain_rows()[0].payload
    assert "sha256:" in payload
    assert "local_protected" in payload


def test_a_digest_that_is_not_a_digest_is_refused(store) -> None:
    with pytest.raises(EvidenceFactError):
        record_evidence(
            store,
            EvidenceKind.ARTIFACT,
            actor=_ACTOR,
            producer=_HOST,
            run_id="run-1",
            facts={
                "artifact_kind": "compiled_prompt",
                "digest": "You are dispatched on issue #7",
                "byte_length": 4096,
                "storage": "local_protected",
            },
        )


# ---------------------------------------------------------------------------
# Evidence never resolves the safest action
# ---------------------------------------------------------------------------


def test_no_evidence_row_may_carry_a_safest_action(store) -> None:
    with pytest.raises(EvidenceFactError):
        record_evidence(
            store,
            EvidenceKind.RECEIPT_FACT,
            actor=_ACTOR,
            producer=_HOST,
            run_id="run-1",
            facts={"fact": "outcome", "value": "ok", "safest_action": "merge"},
        )


def test_a_replayed_record_exposes_no_safest_action(store) -> None:
    _receipt(store)
    record = replay_host_sequence(store).records[0]
    assert not hasattr(record, "safest_action")
    assert "safest_action" not in record.facts


def test_the_evidence_module_never_reaches_the_work_projection() -> None:
    source = pathlib.Path(__file__).resolve().parents[1] / "src" / "kew" / "evidence.py"
    imported = set()
    for node in ast.walk(ast.parse(source.read_text())):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
    assert not [m for m in imported if m.startswith(("kew.service", "kew.serve"))]


# ---------------------------------------------------------------------------
# Deterministic replay (Task 24)
# ---------------------------------------------------------------------------


def test_a_reopened_store_replays_the_same_verified_sequence(tmp_path) -> None:
    db = str(tmp_path / "dispatch.db")
    with SqliteRunStore(db) as writer:
        _receipt(writer, "admitted")
        _change(writer, "a.py", AttemptedChange.FILTERED)
        _change(writer, "b.py", AttemptedChange.PRESERVED)
        emit_evidence(
            writer,
            EvidenceKind.RUNNER_PROGRESS,
            actor=_ACTOR,
            producer=_RUNNER,
            run_id="run-1",
            facts={"phase": "editing"},
        )
        first = replay_host_sequence(writer)
    with SqliteRunStore(db) as reader:
        second = replay_host_sequence(reader)
    assert first.records == second.records
    assert second.chain.ok
    assert second.gaps == ()


def test_replay_can_be_narrowed_to_one_run(store) -> None:
    _receipt(store, "mine", run_id="run-1")
    _receipt(store, "theirs", run_id="run-2")
    replayed = replay_host_sequence(store, run_id="run-2")
    assert [r.facts["value"] for r in replayed.records] == ["theirs"]


def test_a_tampered_ledger_is_reported_by_replay(tmp_path) -> None:
    db = str(tmp_path / "dispatch.db")
    with SqliteRunStore(db) as writer:
        _receipt(writer, "one")
        _receipt(writer, "two")
    import sqlite3

    conn = sqlite3.connect(db)
    conn.execute("UPDATE audit_events SET payload = ? WHERE id = 1", ('{"x": 1}',))
    conn.commit()
    conn.close()
    with SqliteRunStore(db) as reader:
        replayed = replay_host_sequence(reader)
    assert replayed.chain.ok is False
    assert replayed.ok is False


# ---------------------------------------------------------------------------
# Task 24: one host sequence, reopened, chain-verified, replayed
# ---------------------------------------------------------------------------


_DISPOSITIONS = (
    ("filtered.env", AttemptedChange.FILTERED),
    ("reverted.py", AttemptedChange.REVERTED),
    ("dropped.md", AttemptedChange.DROPPED),
    ("refused.sh", AttemptedChange.REFUSED),
    ("preserved.py", AttemptedChange.PRESERVED),
    ("unknown.bin", AttemptedChange.UNAVAILABLE),
)


def _write_host_sequence(store: Any, prompt_text: str) -> None:
    """Admission, the prompt artifact, six attempted changes, then terminal."""
    from kew.sensitive_prompt import prompt_digest

    _receipt(store, "admitted")
    record_evidence(
        store,
        EvidenceKind.ARTIFACT,
        actor=_ACTOR,
        producer=_HOST,
        run_id="run-1",
        facts={
            "artifact_kind": "compiled_prompt",
            "digest": prompt_digest(prompt_text),
            "byte_length": len(prompt_text.encode("utf-8")),
            "storage": "local_protected",
        },
    )
    for subject, disposition in _DISPOSITIONS:
        _change(store, subject, disposition)
    import_runner_evidence(
        store,
        EvidenceKind.RUNNER_USAGE,
        actor=_ACTOR,
        producer=_RUNNER,
        run_id="run-1",
        facts={"metric": "tokens", "value": 4096},
    )
    _receipt(store, "succeeded")


class TestHostSequenceReplay:
    """The whole of Task 24 against one realistic sequence."""

    PROMPT = "You are dispatched on issue #7.\nRotate the token 'hunter2'.\n"

    def _seed(self, tmp_path) -> str:
        db = str(tmp_path / "dispatch.db")
        with SqliteRunStore(db) as writer:
            _write_host_sequence(writer, self.PROMPT)
        return db

    def _replayed(self, tmp_path):
        """Replay through a REOPENED store, never the writer's own handle."""
        with SqliteRunStore(self._seed(tmp_path)) as reader:
            return replay_host_sequence(reader)

    def test_the_reopened_chain_verifies_and_replays_without_gaps(self, tmp_path):
        replayed = self._replayed(tmp_path)
        assert replayed.chain.ok
        assert replayed.chain.count == len(replayed.records)
        assert replayed.gaps == ()
        assert replayed.ok

    def test_the_replay_is_deterministic_across_repeated_reads(self, tmp_path):
        db = self._seed(tmp_path)
        with SqliteRunStore(db) as first_reader:
            first = replay_host_sequence(first_reader)
        with SqliteRunStore(db) as second_reader:
            assert replay_host_sequence(second_reader).records == first.records

    def test_every_disposition_survives_the_round_trip_distinguishably(self, tmp_path):
        from kew.journal_replay import attempted_changes

        replayed = self._replayed(tmp_path)
        changes = attempted_changes(replayed)
        assert changes == tuple(
            (subject, disposition.value) for subject, disposition in _DISPOSITIONS
        )
        assert len({disposition for _, disposition in changes}) == len(AttemptedChange)

    def test_the_host_and_runner_halves_stay_separable(self, tmp_path):
        replayed = self._replayed(tmp_path)
        imported = [r for r in replayed.records if not r.attested_by_host]
        assert [r.kind for r in imported] == [EvidenceKind.RUNNER_USAGE.value]
        assert all(
            r.producer.kind is ProducerKind.HOST
            for r in replayed.records
            if r.attested_by_host
        )

    def test_absent_telemetry_is_named_rather_than_inferred(self, tmp_path):
        replayed = self._replayed(tmp_path)
        assert telemetry_status(replayed) == {
            EvidenceKind.RUNNER_USAGE.value: "present",
            EvidenceKind.RUNNER_PROGRESS.value: "unavailable",
        }

    def test_the_exact_prompt_bytes_reach_no_payload_or_export(self, tmp_path):
        from kew.audit import export_audit_csv, export_audit_json
        from kew.sensitive_prompt import prompt_digest

        with SqliteRunStore(self._seed(tmp_path)) as reader:
            events = reader.list_audit_events(limit=None)
            chained = "".join(row.payload or "" for row in reader.read_chain_rows())
        exported = export_audit_json(events) + export_audit_csv(events)
        assert "hunter2" not in exported
        assert "hunter2" not in chained
        assert prompt_digest(self.PROMPT) in chained

    def test_no_replayed_row_resolves_the_safest_action(self, tmp_path):
        replayed = self._replayed(tmp_path)
        assert all("safest_action" not in record.facts for record in replayed.records)

    @pytest.mark.policy
    def test_the_work_projection_never_imports_the_evidence_module(self):
        root = pathlib.Path(__file__).resolve().parents[1] / "src" / "kew"
        offenders = [
            module.name
            for module in (root / "service").rglob("*.py")
            if "kew.evidence" in module.read_text()
        ]
        assert offenders == []
