"""I07 node 2: the lineage is sealed, and a seal is not a row you can edit.

A receipt says: *this* source, on *this* run and attempt, took *this* trusted
base and *this* preserved package, produced *this* immutable tree, verified it
*this* way — and every audit event up to sequence ``through_seq`` was already
written when it said so.

Two things make that a seal rather than a note.

* **The receipt and its seal event are one transaction.** A receipt without an
  event would be an unattested claim; an event without a receipt would be an
  attestation of nothing. Neither half can exist alone, including across a
  crash.
* **The row is immutable and self-proving.** SQLite refuses an UPDATE or a
  DELETE outright, and the digest is recomputed from the row on every
  verification — so a receipt edited by someone who got past the trigger stops
  verifying instead of quietly reading as evidence.

Everything E5 adds later — commit, head, branch, PR — arrives as chained
attestations after the receipt. None of them rewrite it, and that is asserted
here rather than left as an intention.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from kew.artifact_receipt import (
    RECEIPT_ATTESTED_EVENT,
    RECEIPT_SEALED_EVENT,
    ArtifactLineage,
    ReceiptConflictError,
    ReceiptMissingError,
    ReceiptSealedError,
    ReceiptValueError,
    receipt_digest,
)
from kew.artifact_store import PACKAGE_ARTIFACT_KIND
from kew.audit_chain import entry_hash, verify_chain
from kew.publication_repository import VERIFICATION_EXACT_TREE
from kew.run_store import ArtifactConflictError, SqliteRunStore

_SEALED_AT = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
_PACKAGE = "sha256:" + "a" * 64
_BASE = "b" * 40
_TREE = "c" * 40


def _lineage(**overrides: object) -> ArtifactLineage:
    fields: dict[str, object] = {
        "source_kind": "github",
        "source_id": "acme/widgets",
        "run_id": "run-1",
        "attempt_number": 1,
        "base_sha": _BASE,
        "package_digest": _PACKAGE,
        "tree_sha": _TREE,
        "verification": VERIFICATION_EXACT_TREE,
        "locator": "packages/" + "a" * 64,
    }
    fields.update(overrides)
    return ArtifactLineage(**fields)  # type: ignore[arg-type]


def _store(tmp_path: Path) -> SqliteRunStore:
    return SqliteRunStore(str(tmp_path / "dispatch.db"))


def _seal(store: SqliteRunStore, **overrides: object):
    return store.seal_artifact_receipt(
        lineage=_lineage(**overrides), sealed_at=_SEALED_AT
    )


def _events(store: SqliteRunStore, event: str) -> list[sqlite3.Row]:
    return store._conn.execute(
        "SELECT * FROM audit_events WHERE event = ? ORDER BY id", (event,)
    ).fetchall()


class TestTheLineageIsWholeOrItIsNotSealed:
    def test_a_sealed_receipt_binds_every_link(self, tmp_path: Path):
        with _store(tmp_path) as store:
            receipt = _seal(store)

        assert receipt.source_kind == "github"
        assert receipt.source_id == "acme/widgets"
        assert receipt.run_id == "run-1"
        assert receipt.attempt_number == 1
        assert receipt.base_sha == _BASE
        assert receipt.package_digest == _PACKAGE
        assert receipt.tree_sha == _TREE
        assert receipt.verification == VERIFICATION_EXACT_TREE
        assert receipt.through_seq > 0
        assert len(receipt.chain_head) == 64

    @pytest.mark.parametrize(
        "override",
        [
            {"package_digest": "not-a-digest"},
            {"package_digest": "sha256:zzz"},
            {"base_sha": "HEAD"},
            {"tree_sha": ""},
            {"attempt_number": 0},
            {"run_id": ""},
            {"source_id": ""},
            {"verification": ""},
        ],
    )
    def test_a_lineage_with_a_broken_link_is_refused(self, tmp_path, override):
        with _store(tmp_path) as store:
            with pytest.raises(ReceiptValueError):
                _seal(store, **override)
            assert _events(store, RECEIPT_SEALED_EVENT) == []


class TestTheReceiptAndItsSealEventArriveTogether:
    def test_the_receipt_records_the_head_of_the_prefix_it_seals(self, tmp_path: Path):
        with _store(tmp_path) as store:
            store.append_audit_event(
                timestamp="2026-08-18T11:00:00+00:00", event="dispatch_started"
            )
            receipt = _seal(store)
            rows = store.read_chain_rows()

        sealed = [row for row in rows if row.id == receipt.through_seq]
        assert len(sealed) == 1
        assert sealed[0].event == RECEIPT_SEALED_EVENT
        assert rows[-1].id == receipt.through_seq
        assert entry_hash(sealed[0]) == receipt.chain_head
        assert verify_chain(rows).ok

    def test_the_seal_event_carries_the_receipt_digest(self, tmp_path: Path):
        import json

        with _store(tmp_path) as store:
            receipt = _seal(store)
            payload = json.loads(_events(store, RECEIPT_SEALED_EVENT)[0]["payload"])

        assert payload["receipt_digest"] == receipt.receipt_digest
        assert payload["tree_sha"] == _TREE
        assert payload["package_digest"] == _PACKAGE

    def test_a_failed_receipt_insert_leaves_no_seal_event_behind(self, tmp_path: Path):
        with _store(tmp_path) as store:
            original = store._execute

            def refuse(sql, params=None):
                if "artifact_receipt" in sql:
                    raise sqlite3.OperationalError("disk I/O error")
                return original(sql, params)

            store._execute = refuse  # type: ignore[method-assign]
            with pytest.raises(sqlite3.OperationalError):
                _seal(store)
            store._execute = original  # type: ignore[method-assign]

            assert _events(store, RECEIPT_SEALED_EVENT) == []
            assert store.get_artifact_receipt(run_id="run-1", attempt_number=1) is None
            assert store.read_chain_rows() == []


class TestSealingIsIdempotentAndImmutable:
    def test_sealing_the_same_lineage_twice_is_one_receipt(self, tmp_path: Path):
        with _store(tmp_path) as store:
            first = _seal(store)
            second = _seal(store)

            assert first == second
            assert len(_events(store, RECEIPT_SEALED_EVENT)) == 1
            assert len(store.list_artifact_receipts(run_id="run-1")) == 1

    def test_a_different_tree_for_the_same_attempt_is_refused(self, tmp_path: Path):
        with _store(tmp_path) as store:
            first = _seal(store)
            with pytest.raises(ReceiptConflictError):
                _seal(store, tree_sha="d" * 40)

            assert store.get_artifact_receipt(run_id="run-1", attempt_number=1) == first
            assert len(_events(store, RECEIPT_SEALED_EVENT)) == 1

    def test_a_sealed_receipt_cannot_be_updated_or_deleted(self, tmp_path: Path):
        with _store(tmp_path) as store:
            _seal(store)
            for sql in (
                "UPDATE artifact_receipt SET tree_sha = 'e'",
                "DELETE FROM artifact_receipt",
            ):
                with pytest.raises(sqlite3.IntegrityError):
                    store._conn.execute(sql)
                store._conn.rollback()

            assert (
                store.get_artifact_receipt(run_id="run-1", attempt_number=1).tree_sha
                == _TREE
            )

    def test_a_second_attempt_seals_its_own_receipt(self, tmp_path: Path):
        with _store(tmp_path) as store:
            first = _seal(store)
            second = _seal(store, attempt_number=2, tree_sha="d" * 40)

            assert second.through_seq > first.through_seq
            assert len(store.list_artifact_receipts(run_id="run-1")) == 2


class TestVerificationRecomputesRatherThanBelieves:
    def test_an_untouched_receipt_verifies(self, tmp_path: Path):
        with _store(tmp_path) as store:
            _seal(store)
            verdict = store.verify_artifact_receipt(run_id="run-1", attempt_number=1)

        assert verdict.ok, verdict.detail

    def test_a_forged_receipt_row_stops_verifying(self, tmp_path: Path):
        with _store(tmp_path) as store:
            _seal(store)
            # Past the trigger, deliberately: the digest is the second line of
            # defence, and it is the one that has to hold on a restored or
            # hand-edited database where the trigger was never in the way.
            store._conn.execute("DROP TRIGGER artifact_receipt_no_update")
            store._conn.execute("UPDATE artifact_receipt SET tree_sha = 'ffff'")
            store._conn.commit()
            verdict = store.verify_artifact_receipt(run_id="run-1", attempt_number=1)

        assert not verdict.ok
        assert "digest" in verdict.detail

    def test_a_tampered_audit_prefix_stops_the_receipt_verifying(self, tmp_path: Path):
        with _store(tmp_path) as store:
            store.append_audit_event(
                timestamp="2026-08-18T11:00:00+00:00", event="dispatch_started"
            )
            _seal(store)
            store._conn.execute(
                "UPDATE audit_events SET event = 'rewritten' WHERE id = 1"
            )
            store._conn.commit()
            verdict = store.verify_artifact_receipt(run_id="run-1", attempt_number=1)

        assert not verdict.ok
        assert "chain" in verdict.detail or "prefix" in verdict.detail

    def test_an_absent_receipt_verifies_as_absent_not_as_valid(self, tmp_path: Path):
        with _store(tmp_path) as store:
            verdict = store.verify_artifact_receipt(run_id="ghost", attempt_number=1)

        assert not verdict.ok
        assert "no receipt" in verdict.detail

    def test_the_digest_covers_every_field_of_the_lineage(self, tmp_path: Path):
        base = receipt_digest(_lineage(), sealed_at="2026-08-18T12:00:00+00:00")
        for override in (
            {"source_kind": "linear"},
            {"source_id": "acme/other"},
            {"run_id": "run-2"},
            {"attempt_number": 2},
            {"base_sha": "d" * 40},
            {"package_digest": "sha256:" + "d" * 64},
            {"tree_sha": "d" * 40},
            {"verification": "eyeballed"},
            {"locator": "packages/" + "d" * 64},
        ):
            other = receipt_digest(
                _lineage(**override), sealed_at="2026-08-18T12:00:00+00:00"
            )
            assert other != base, override
        assert receipt_digest(_lineage(), sealed_at="2026-08-18T12:00:01+00:00") != base


class TestLaterAttestationsChainAfterTheReceipt:
    def test_publication_judgment_and_sync_events_never_rewrite_the_receipt(
        self, tmp_path: Path
    ):
        with _store(tmp_path) as store:
            receipt = _seal(store)
            for kind in ("publication", "judgment", "sync"):
                store.attest_artifact_receipt(
                    run_id="run-1",
                    attempt_number=1,
                    kind=kind,
                    timestamp="2026-08-18T13:00:00+00:00",
                    payload={"detail": kind},
                )

            after = store.get_artifact_receipt(run_id="run-1", attempt_number=1)
            attestations = store.artifact_receipt_attestations(
                run_id="run-1", attempt_number=1
            )
            rows = store.read_chain_rows()

        assert after == receipt
        assert [a.kind for a in attestations] == ["publication", "judgment", "sync"]
        assert all(a.seq > receipt.through_seq for a in attestations)
        assert verify_chain(rows).ok

    def test_an_attestation_binds_the_receipt_it_is_about(self, tmp_path: Path):
        with _store(tmp_path) as store:
            receipt = _seal(store)
            store.attest_artifact_receipt(
                run_id="run-1",
                attempt_number=1,
                kind="publication",
                timestamp="2026-08-18T13:00:00+00:00",
                payload={"commit_sha": "d" * 40},
            )
            attestation = store.artifact_receipt_attestations(
                run_id="run-1", attempt_number=1
            )[0]

        assert attestation.payload["receipt_digest"] == receipt.receipt_digest
        assert attestation.payload["through_seq"] == receipt.through_seq
        assert attestation.payload["commit_sha"] == "d" * 40

    def test_a_terminal_attestation_closes_the_receipt(self, tmp_path: Path):
        with _store(tmp_path) as store:
            _seal(store)
            store.attest_artifact_receipt(
                run_id="run-1",
                attempt_number=1,
                kind="published",
                timestamp="2026-08-18T13:00:00+00:00",
                terminal=True,
            )
            with pytest.raises(ReceiptSealedError):
                store.attest_artifact_receipt(
                    run_id="run-1",
                    attempt_number=1,
                    kind="publication",
                    timestamp="2026-08-18T14:00:00+00:00",
                )

            assert len(_events(store, RECEIPT_ATTESTED_EVENT)) == 1

    def test_an_attestation_without_a_receipt_is_refused(self, tmp_path: Path):
        with _store(tmp_path) as store:
            with pytest.raises(ReceiptMissingError):
                store.attest_artifact_receipt(
                    run_id="run-1",
                    attempt_number=1,
                    kind="publication",
                    timestamp="2026-08-18T13:00:00+00:00",
                )
            assert _events(store, RECEIPT_ATTESTED_EVENT) == []

    def test_an_attestation_cannot_smuggle_a_different_lineage_in(self, tmp_path: Path):
        with _store(tmp_path) as store:
            receipt = _seal(store)
            store.attest_artifact_receipt(
                run_id="run-1",
                attempt_number=1,
                kind="publication",
                timestamp="2026-08-18T13:00:00+00:00",
                payload={"receipt_digest": "forged", "tree_sha": "d" * 40},
            )
            attestation = store.artifact_receipt_attestations(
                run_id="run-1", attempt_number=1
            )[0]

        assert attestation.payload["receipt_digest"] == receipt.receipt_digest


class TestTheReceiptTableIsMigratedIn:
    def test_a_v45_ledger_gains_the_table_without_losing_its_chain(
        self, tmp_path: Path
    ):
        db = tmp_path / "dispatch.db"
        with SqliteRunStore(str(db)) as store:
            store.append_audit_event(
                timestamp="2026-08-18T11:00:00+00:00", event="dispatch_started"
            )
        raw = sqlite3.connect(db)
        raw.execute("DROP TRIGGER artifact_receipt_no_update")
        raw.execute("DROP TRIGGER artifact_receipt_no_delete")
        raw.execute("DROP TABLE artifact_receipt")
        raw.execute("UPDATE schema_version SET version = 45 WHERE id = 1")
        raw.commit()
        raw.close()

        with SqliteRunStore(str(db)) as store:
            rows = store.read_chain_rows()
            receipt = _seal(store)
            verdict = store.verify_artifact_receipt(run_id="run-1", attempt_number=1)

        assert [row.event for row in rows] == ["dispatch_started"]
        assert receipt.through_seq == 2
        assert verdict.ok, verdict.detail


class TestTheDurableIndexIsImmutableToo:
    """I07: the row that says WHERE the package is cannot be repointed."""

    def _record(self, store, **overrides):
        fields = {
            "run_id": "run-1",
            "attempt_number": 1,
            "kind": PACKAGE_ARTIFACT_KIND,
            "digest": _PACKAGE,
            "byte_length": 4096,
            "locator": "packages/" + "a" * 64,
            "created_at": _SEALED_AT,
        }
        fields.update(overrides)
        return store.record_artifact(**fields)

    def test_recording_the_same_placement_twice_is_idempotent(self, tmp_path: Path):
        with _store(tmp_path) as store:
            self._record(store)
            self._record(store)
            assert len(store.list_artifacts(run_id="run-1")) == 1

    @pytest.mark.parametrize(
        "override",
        [
            {"locator": "packages/" + "b" * 64},
            {"byte_length": 4097},
            {"digest": "sha256:" + "b" * 64},
        ],
    )
    def test_a_placement_that_changed_is_refused_not_overwritten(
        self, tmp_path, override
    ):
        with _store(tmp_path) as store:
            first = self._record(store)
            with pytest.raises(ArtifactConflictError):
                self._record(store, **override)
            assert store.list_artifacts(run_id="run-1") == [first]

    def test_retention_can_remove_the_bytes_without_unsealing_the_receipt(
        self, tmp_path: Path
    ):
        with _store(tmp_path) as store:
            self._record(store)
            receipt = _seal(store)
            store.set_artifact_retention(
                run_id="run-1",
                attempt_number=1,
                kind=PACKAGE_ARTIFACT_KIND,
                terminal_at=_SEALED_AT,
            )
            expired = store.list_expired_artifacts(
                now=datetime(2027, 1, 1, tzinfo=UTC), kind=PACKAGE_ARTIFACT_KIND
            )
            store.mark_artifact_purged(
                run_id="run-1",
                attempt_number=1,
                kind=PACKAGE_ARTIFACT_KIND,
                purged_at=datetime(2027, 1, 1, tzinfo=UTC),
                reason="retention",
            )
            purged = store.get_artifact(
                run_id="run-1", attempt_number=1, kind=PACKAGE_ARTIFACT_KIND
            )
            verdict = store.verify_artifact_receipt(run_id="run-1", attempt_number=1)

        assert [a.digest for a in expired] == [_PACKAGE]
        assert purged.digest == _PACKAGE and not purged.available
        assert purged.purge_reason == "retention"
        assert verdict.ok, verdict.detail
        assert receipt.package_digest == _PACKAGE


class TestARetryDoesNotRestampTheSeal:
    def test_re_sealing_later_returns_the_receipt_that_already_happened(
        self, tmp_path: Path
    ):
        with _store(tmp_path) as store:
            first = _seal(store)
            again = store.seal_artifact_receipt(
                lineage=_lineage(), sealed_at=datetime(2026, 9, 1, tzinfo=UTC)
            )

            assert again == first
            assert again.sealed_at == first.sealed_at
            assert len(_events(store, RECEIPT_SEALED_EVENT)) == 1
            assert store.verify_artifact_receipt(run_id="run-1", attempt_number=1).ok
