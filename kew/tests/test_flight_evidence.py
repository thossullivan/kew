"""I16 node 1: the Flight Recorder reads evidence, not a mutable repository.

The recorder used to answer "what happened in this run?" out of two places
that could change after the run ended: a speculative tree of git snapshots
captured from the agent's own worktree, and a diff computed against whatever
the host repository directory happened to contain when the page was opened.

Everything here is a statement about the two durable things I06 and I07
already produce — the ``audit_events`` host sequence, and the sealed receipt
whose ``through_seq`` names the exact prefix of that sequence it covers,
addressing a preserved work package by digest.

Three properties carry the weight:

* **The prefix is the seal.** Events at or before ``through_seq`` are covered
  by the receipt. Later chained facts are shown, but labelled as later.
* **A diff is bound to the artifact or it is refused.** Not "usually derived
  from" — the package is opened by the receipt's own digest, and its base has
  to be the base the receipt sealed.
* **Missing is missing.** Absent runner telemetry is reported as unavailable
  and never inferred, never defaulted to zero, and never borrowed from
  another run.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from kew.artifact_receipt import seal_package_receipt
from kew.artifact_store import PackageStore, default_root
from kew.audit import Actor
from kew.evidence import (
    AttemptedChange,
    EvidenceKind,
    Producer,
    ProducerKind,
    import_runner_evidence,
    record_evidence,
)
from kew.flight.evidence import (
    AVAILABLE,
    UNAVAILABLE,
    ChangeKind,
    EventOrigin,
    TelemetryMode,
    artifact_node_id,
)
from kew.publication_artifact import BLOBS_DIR, MANIFEST_NAME, WORKSPACE_SCOPE
from kew.publication_repository import VERIFICATION_EXACT_TREE
from kew.run_store import SqliteRunStore
from kew.service.runs import get_diff, get_flight_timeline

_ACTOR = Actor("human", "tester")
_HOST = Producer(kind=ProducerKind.HOST, name="kew.dispatch", version="1.2.3")
_RUNNER = Producer(kind=ProducerKind.RUNNER, name="claude-code", version="9.9")
_BASE = "b" * 40
_TREE = "c" * 40
_SEALED_AT = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)


@dataclass(frozen=True)
class _Promoted:
    """The shape :func:`seal_package_receipt` promotes from (PromotedLike)."""

    base_sha: str = _BASE
    tree_sha: str = _TREE
    verification: str = VERIFICATION_EXACT_TREE


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "dispatch.db"


@pytest.fixture
def store(db: Path) -> Any:
    with SqliteRunStore(str(db)) as opened:
        yield opened


def _receipt_fact(store: Any, value: str = "ok", **kwargs: Any) -> int:
    return record_evidence(
        store,
        EvidenceKind.RECEIPT_FACT,
        actor=_ACTOR,
        producer=_HOST,
        run_id=kwargs.pop("run_id", "run-1"),
        facts={"fact": "outcome", "value": value},
        **kwargs,
    )


def _change(store: Any, subject: str, disposition: str, **kwargs: Any) -> int:
    return record_evidence(
        store,
        EvidenceKind.ATTEMPTED_CHANGE,
        actor=_ACTOR,
        producer=_HOST,
        run_id=kwargs.pop("run_id", "run-1"),
        facts={"subject": subject, "disposition": disposition},
        **kwargs,
    )


def _progress(store: Any, phase: str, *, run_id: str = "run-1") -> None:
    import_runner_evidence(
        store,
        EvidenceKind.RUNNER_PROGRESS,
        actor=_ACTOR,
        producer=_RUNNER,
        run_id=run_id,
        facts={"phase": phase},
    )


def _usage(store: Any, metric: str, value: int, *, run_id: str = "run-1") -> None:
    import_runner_evidence(
        store,
        EvidenceKind.RUNNER_USAGE,
        actor=_ACTOR,
        producer=_RUNNER,
        run_id=run_id,
        facts={"metric": metric, "value": value, "unit": "tokens"},
    )


def _write_package(where: Path, files: dict[str, bytes], deletions: tuple[str, ...]):
    """Write a well-formed producer artifact at *where* and read it back."""
    from kew.publication_artifact import read_artifact

    blobs = where / BLOBS_DIR
    blobs.mkdir(parents=True)
    entries = []
    for path, payload in files.items():
        digest = hashlib.sha256(payload).hexdigest()
        (blobs / digest).write_bytes(payload)
        entries.append({"path": path, "mode": "100644", "sha256": digest})
    (where / MANIFEST_NAME).write_text(
        json.dumps(
            {
                "base": _BASE,
                "entries": entries,
                "deletions": list(deletions),
                "scope": WORKSPACE_SCOPE,
            }
        ),
        encoding="utf-8",
    )
    return read_artifact(where)


def _seal(
    store: Any,
    project_root: Path,
    *,
    files: dict[str, bytes] | None = None,
    deletions: tuple[str, ...] = (),
    run_id: str = "run-1",
    source_kind: str = "github",
    source_id: str = "acme/widgets",
) -> Any:
    """Preserve a real package under *project_root* and seal its lineage."""
    produced = project_root / f"produced-{run_id}"
    artifact = _write_package(
        produced, files or {"src/main.py": b"print(1)\n"}, deletions
    )
    packages = PackageStore(default_root(project_root))
    preserved = packages.preserve(artifact)
    return seal_package_receipt(
        store,
        package=preserved,
        promoted=_Promoted(),
        source_kind=source_kind,
        source_id=source_id,
        run_id=run_id,
        attempt_number=1,
        sealed_at=_SEALED_AT,
    )


def _dimension(dto: Any, name: str) -> Any:
    return next(row for row in dto.availability if row.dimension == name)


class TestTheHostSequenceIsTheTimeline:
    def test_events_are_ordered_by_ledger_position_not_by_clock(self, store):
        first = _receipt_fact(store, "one", now=datetime(2026, 8, 18, 23, tzinfo=UTC))
        second = _receipt_fact(store, "two", now=datetime(2026, 8, 18, 1, tzinfo=UTC))

        dto = get_flight_timeline(store, "run-1")

        assert [event.seq for event in dto.events] == [first, second]
        assert first < second

    def test_the_receipt_prefix_splits_sealed_from_later(self, store, tmp_path):
        _receipt_fact(store, "before")
        _change(store, ".env", AttemptedChange.FILTERED)
        receipt = _seal(store, tmp_path)
        _receipt_fact(store, "after")

        dto = get_flight_timeline(store, "run-1")
        sealed = [event.seq for event in dto.events if event.sealed]
        later = [event.seq for event in dto.events if not event.sealed]

        assert dto.through_seq == receipt.through_seq
        assert dto.chain_head == receipt.chain_head
        assert sealed and max(sealed) <= receipt.through_seq
        assert later and min(later) > receipt.through_seq

    def test_a_restart_replays_the_identical_timeline(self, db, tmp_path):
        with SqliteRunStore(str(db)) as store:
            _receipt_fact(store, "one")
            _seal(store, tmp_path)
            _progress(store, "planning")
            before = get_flight_timeline(store, "run-1")

        with SqliteRunStore(str(db)) as reopened:
            after = get_flight_timeline(reopened, "run-1")

        assert after == before

    def test_a_run_with_no_evidence_says_so_rather_than_inventing_any(self, store):
        dto = get_flight_timeline(store, "run-nothing")

        assert dto.events == []
        assert dto.sealed is False
        assert _dimension(dto, "artifact_seal").availability == UNAVAILABLE


class TestLaterChainedEventsAreShownAsLater:
    def test_an_attestation_after_the_seal_is_carried_and_labelled(
        self, store, tmp_path
    ):
        receipt = _seal(store, tmp_path)
        seq = store.attest_artifact_receipt(
            run_id="run-1",
            attempt_number=1,
            kind="published",
            timestamp=_SEALED_AT,
            payload={"pr_url": "https://example.invalid/pr/1"},
            terminal=True,
        )

        dto = get_flight_timeline(store, "run-1")
        attestation = next(
            event for event in dto.events if event.origin == EventOrigin.ATTESTATION
        )

        assert attestation.seq == seq > receipt.through_seq
        assert attestation.sealed is False
        assert attestation.terminal is True
        assert attestation.facts["attestation_kind"] == "published"

    def test_attestations_interleave_with_evidence_in_one_sequence(
        self, store, tmp_path
    ):
        _seal(store, tmp_path)
        store.attest_artifact_receipt(
            run_id="run-1", attempt_number=1, kind="pushed", timestamp=_SEALED_AT
        )
        _receipt_fact(store, "later")

        dto = get_flight_timeline(store, "run-1")

        assert [event.seq for event in dto.events] == sorted(
            event.seq for event in dto.events
        )
        assert {event.origin for event in dto.events} == {
            EventOrigin.EVIDENCE,
            EventOrigin.ATTESTATION,
        }


class TestProducerProvenanceSurvivesTheProjection:
    def test_host_and_runner_rows_stay_distinguishable(self, store):
        _receipt_fact(store, "host-derived")
        _progress(store, "planning")

        dto = get_flight_timeline(store, "run-1")
        by_kind = {event.kind: event for event in dto.events}

        host = by_kind[EvidenceKind.RECEIPT_FACT.value]
        runner = by_kind[EvidenceKind.RUNNER_PROGRESS.value]
        assert host.producer_kind == ProducerKind.HOST
        assert host.producer_name == "kew.dispatch"
        assert host.attested_by_host is True
        assert runner.producer_kind == ProducerKind.RUNNER
        assert runner.producer_name == "claude-code"
        assert runner.producer_version == "9.9"
        assert runner.attested_by_host is False


class TestRunnerTelemetryIsExplicitOrAbsent:
    def test_structured_telemetry_is_reported_as_structured(self, store):
        _progress(store, "planning")
        _usage(store, "input_tokens", 120)

        dto = get_flight_timeline(store, "run-1")

        assert dto.runner_telemetry == TelemetryMode.STRUCTURED
        assert _dimension(dto, "runner_usage").availability == AVAILABLE
        assert _dimension(dto, "runner_progress").availability == AVAILABLE

    def test_activity_only_telemetry_does_not_claim_metrics(self, store):
        _progress(store, "planning")

        dto = get_flight_timeline(store, "run-1")

        assert dto.runner_telemetry == TelemetryMode.ACTIVITY_ONLY
        usage = _dimension(dto, "runner_usage")
        assert usage.availability == UNAVAILABLE
        assert usage.provenance == "unobserved"
        assert _dimension(dto, "runner_progress").availability == AVAILABLE

    def test_missing_telemetry_is_unavailable_never_zero(self, store):
        _receipt_fact(store, "ok")

        dto = get_flight_timeline(store, "run-1")

        assert dto.runner_telemetry == TelemetryMode.MISSING
        assert _dimension(dto, "runner_usage").availability == UNAVAILABLE
        assert _dimension(dto, "runner_progress").availability == UNAVAILABLE

    def test_telemetry_is_never_borrowed_from_another_run(self, store):
        _receipt_fact(store, "ok", run_id="run-1")
        _progress(store, "planning", run_id="run-2")
        _usage(store, "input_tokens", 999, run_id="run-2")

        dto = get_flight_timeline(store, "run-1")

        assert dto.runner_telemetry == TelemetryMode.MISSING
        assert all(event.seq is not None for event in dto.events)
        assert [event.kind for event in dto.events] == [EvidenceKind.RECEIPT_FACT.value]


class TestIdentityIsDurableNotNumeric:
    def test_a_linear_item_keeps_its_own_id_with_no_numeric_shim(self, store, tmp_path):
        _receipt_fact(store, "ok", item_id="SFD-12")
        _seal(store, tmp_path, source_kind="linear", source_id="SFD-12")

        dto = get_flight_timeline(store, "run-1")

        assert dto.item_id == "SFD-12"
        assert dto.issue_num is None
        assert dto.source_tracker == "linear"
        assert dto.source_id == "SFD-12"

    def test_a_github_item_carries_both_ids(self, store, tmp_path):
        _receipt_fact(store, "ok", item_id="312", issue_num=312)
        _seal(store, tmp_path)

        dto = get_flight_timeline(store, "run-1")

        assert dto.item_id == "312"
        assert dto.issue_num == 312
        assert dto.source_tracker == "github"


class TestGapsAreReportedNotDropped:
    def test_an_unreadable_evidence_row_replays_as_a_gap(self, store):
        _receipt_fact(store, "ok")
        bad = store.append_audit_event(
            timestamp=_SEALED_AT.isoformat(),
            event=EvidenceKind.RECEIPT_FACT.value,
            run_id="run-1",
            payload={"evidence_schema": 999, "facts": {}},
        )

        dto = get_flight_timeline(store, "run-1")

        assert [gap.seq for gap in dto.gaps] == [bad]
        assert dto.gaps[0].reason == "unsupported_schema"
        assert _dimension(dto, "host_sequence").availability == UNAVAILABLE

    def test_a_clean_sequence_reports_no_gaps(self, store):
        _receipt_fact(store, "ok")

        dto = get_flight_timeline(store, "run-1")

        assert dto.gaps == []
        assert dto.chain_verified is True
        assert _dimension(dto, "host_sequence").availability == AVAILABLE


class TestTheDiffIsBoundToTheArtifactOrRefused:
    def test_the_sealed_artifact_node_yields_the_packages_own_changes(
        self, store, tmp_path
    ):
        receipt = _seal(
            store,
            tmp_path,
            files={"src/main.py": b"print(1)\n", "docs/a.md": b"# a\n"},
            deletions=("old.py",),
        )

        dto = get_diff(store, "run-1", artifact_node_id(receipt), tmp_path)

        assert dto is not None
        assert dto.package_digest == receipt.package_digest
        assert dto.base_sha == _BASE and dto.tree_sha == _TREE
        assert dto.receipt_digest == receipt.receipt_digest
        changed = {f.path: f for f in dto.files if f.change == ChangeKind.CHANGED}
        deleted = [f.path for f in dto.files if f.change == ChangeKind.DELETED]
        assert set(changed) == {"src/main.py", "docs/a.md"}
        assert changed["src/main.py"].text == "print(1)\n"
        assert deleted == ["old.py"]

    def test_the_diff_names_the_receipt_it_is_bound_to_in_its_text(
        self, store, tmp_path
    ):
        receipt = _seal(store, tmp_path)

        dto = get_diff(store, "run-1", artifact_node_id(receipt), tmp_path)

        assert dto is not None
        assert receipt.package_digest in dto.text
        assert _BASE in dto.text and _TREE in dto.text
        assert "src/main.py" in dto.text

    def test_a_node_that_is_not_the_sealed_artifact_is_refused(self, store, tmp_path):
        _seal(store, tmp_path)

        assert get_diff(store, "run-1", "run-1:0", tmp_path) is None
        assert get_diff(store, "run-1", "", tmp_path) is None
        assert get_diff(store, "run-1", "artifact:whatever", tmp_path) is None

    def test_a_run_with_no_sealed_receipt_has_no_diff(self, store, tmp_path):
        _receipt_fact(store, "ok")

        assert get_diff(store, "run-1", "run-1:1", tmp_path) is None

    def test_a_package_edited_after_placement_is_refused_not_shown(
        self, store, tmp_path
    ):
        receipt = _seal(store, tmp_path)
        placed = (
            default_root(tmp_path)
            / "packages"
            / (receipt.package_digest.removeprefix("sha256:"))
        )
        blob = next((placed / BLOBS_DIR).iterdir())
        blob.chmod(0o600)
        blob.write_bytes(b"tampered\n")

        dto = get_diff(store, "run-1", artifact_node_id(receipt), tmp_path)

        assert dto is None

    def test_a_missing_package_root_is_unavailable_not_empty(self, store, tmp_path):
        receipt = _seal(store, tmp_path)

        dto = get_diff(store, "run-1", artifact_node_id(receipt), tmp_path / "nope")

        assert dto is None

    def test_the_timeline_says_a_diff_is_available_only_when_it_is(
        self, store, tmp_path
    ):
        receipt = _seal(store, tmp_path)

        without = get_flight_timeline(store, "run-1")
        with_root = get_flight_timeline(store, "run-1", project_root=tmp_path)

        assert _dimension(without, "artifact_diff").availability == UNAVAILABLE
        assert _dimension(with_root, "artifact_diff").availability == AVAILABLE
        assert with_root.artifact_node_id == artifact_node_id(receipt)

    def test_line_counts_are_unavailable_from_this_route(self, store, tmp_path):
        receipt = _seal(store, tmp_path)

        dto = get_diff(store, "run-1", artifact_node_id(receipt), tmp_path)

        assert dto is not None
        counts = next(
            row for row in dto.availability if row.dimension == "diff_line_counts"
        )
        assert counts.availability == UNAVAILABLE
        assert "base" in counts.detail

    def test_binary_content_is_labelled_rather_than_mangled(self, store, tmp_path):
        receipt = _seal(store, tmp_path, files={"logo.png": b"\x89PNG\xff\xfe"})

        dto = get_diff(store, "run-1", artifact_node_id(receipt), tmp_path)

        assert dto is not None
        entry = dto.files[0]
        assert entry.text is None
        assert entry.text_availability == UNAVAILABLE
        assert entry.byte_length == 6


class TestNoMutableRepositoryIsConsulted:
    def test_the_snapshot_module_is_gone(self):
        with pytest.raises(ModuleNotFoundError):
            __import__("kew.flight.snapshot")

    def test_the_read_path_imports_no_subprocess_surface(self):
        import kew.flight.evidence as evidence_module
        import kew.flight.timeline as timeline_module
        import kew.service.runs as runs_module

        for module in (evidence_module, timeline_module, runs_module):
            source = Path(module.__file__ or "").read_text()
            assert "subprocess" not in source
            assert "subprocess_utils" not in source

    def test_reads_still_work_with_every_subprocess_denied(
        self, store, tmp_path, monkeypatch
    ):
        import kew.subprocess_utils as subprocess_utils

        receipt = _seal(store, tmp_path)

        def _refuse(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("a Flight read must never shell out")

        monkeypatch.setattr(subprocess_utils, "run", _refuse)

        dto = get_diff(store, "run-1", artifact_node_id(receipt), tmp_path)
        timeline = get_flight_timeline(store, "run-1", project_root=tmp_path)

        assert dto is not None and dto.files
        assert timeline.sealed is True
