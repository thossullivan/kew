"""The certificate evidence carrier and its fail-closed resolver (P1.1, #1615).

Three properties are load-bearing here, and each has an attack case below:

* **The carrier is append-only.** Envelopes are immutable blobs and the
  per-subject manifest only ever grows. A rewrite, a replay of an envelope
  already recorded, a deletion, or a write against a moved ref head is
  refused — never silently applied over the top of prior evidence.
* **``certification_ready`` is not ``certified``.** They are distinct envelope
  kinds with distinct semantics: build acceptance can never resolve to a
  journey certificate, only a ``journey_certificate`` bound to an exact lane
  can, and only once its append is anchored in the audit chain.
* **Absent evidence is not "uncertified".** A ref that cannot be read, was
  never fetched, or whose reading is stale resolves to ``unknown`` with an
  unavailable/stale observation. Only a readable ref that genuinely has no
  entry for the subject is truthfully ``uncertified``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from kew import subprocess_utils
from kew.audit_chain import Anchor, anchor_hash, entry_hash, verify_chain
from kew.certificate_ref import (
    CERTIFICATE_REF,
    KIND_CERTIFICATE,
    KIND_READY,
    KIND_STATUS,
    CertificateConflict,
    CertificateEnvelope,
    CertificateRefused,
    append_certificate,
    read_manifest,
    ref_head,
    resolve_certificate,
)
from kew.run_store import SqliteRunStore

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="the carrier is a git ref, as it is in the image",
)

SUBJECT = "a" * 40
OTHER_SUBJECT = "b" * 40
NOW = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _git_on_the_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    """The carrier goes through the subprocess chokepoint, whose allowlist
    preflight normally fills. ``setitem`` restores the global afterwards."""
    resolved = shutil.which("git")
    assert resolved is not None  # the module-level skipif already guarantees it
    monkeypatch.setitem(subprocess_utils.TOOL_PATHS, "git", resolved)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "kew"
    path.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "kew@example.invalid"],
        check=True,
    )
    subprocess.run(["git", "-C", str(path), "config", "user.name", "kew"], check=True)
    return path


class FakeLedger:
    """The narrow slice of the audit store the carrier actually binds to."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def append_audit_event(self, **kwargs: Any) -> int:
        row_id = len(self.rows) + 1
        self.rows.append({"id": row_id, **kwargs})
        return row_id


def _ready(**overrides: Any) -> CertificateEnvelope:
    fields: dict[str, Any] = {
        "kind": KIND_READY,
        "subject": SUBJECT,
        "status": "build_accepted",
        "lane": "claude-code/github",
        "fingerprint": "fp-1",
        "issued_at": "2026-08-24T11:00:00+00:00",
        "actor": "release",
    }
    fields.update(overrides)
    return CertificateEnvelope(**fields)


def _certificate(**overrides: Any) -> CertificateEnvelope:
    fields: dict[str, Any] = {
        "kind": KIND_CERTIFICATE,
        "subject": SUBJECT,
        "status": "certified",
        "lane": "claude-code/github",
        "fingerprint": "fp-1",
        "issued_at": "2026-08-24T11:30:00+00:00",
        "actor": "release",
        "body": {"live_trace": "run-7", "exclusions": ["linear"]},
    }
    fields.update(overrides)
    return CertificateEnvelope(**fields)


def _anchors_for(ledger: FakeLedger) -> list[Anchor]:
    """One valid v1 anchor covering every row *ledger* holds."""
    anchor = Anchor(
        id=len(ledger.rows),
        entry_hash="e" * 64,
        count=len(ledger.rows),
        timestamp="2026-08-24T11:59:00+00:00",
        prev="0" * 64,
    )
    assert anchor_hash(anchor)  # the chain helper accepts the record shape
    return [anchor]


def _write(repo: Path, envelope: CertificateEnvelope, ledger: FakeLedger) -> Any:
    return append_certificate(repo, envelope, ledger=ledger, now=NOW)


def _resolve(repo: Path, ledger: FakeLedger, **kwargs: Any) -> Any:
    params: dict[str, Any] = {
        "audit_rows": ledger.rows,
        "anchors": _anchors_for(ledger),
        "fetched_at": NOW,
        "now": NOW,
    }
    params.update(kwargs)
    return resolve_certificate(repo, SUBJECT, **params)


# --------------------------------------------------------------------------- #
# Carrier: round-trip, CAS/fast-forward, and every refusal
# --------------------------------------------------------------------------- #


def test_append_then_read_round_trips_on_the_evidence_ref(repo: Path) -> None:
    ledger = FakeLedger()

    result = _write(repo, _certificate(), ledger)

    assert result.ref == CERTIFICATE_REF
    assert result.ref_before is None
    assert result.ref_after == ref_head(repo)
    manifest = read_manifest(repo, SUBJECT)
    assert manifest is not None
    assert [e.kind for e in manifest.entries] == [KIND_CERTIFICATE]
    assert manifest.entries[0].digest == result.digest
    assert manifest.entries[0].predecessor is None
    assert manifest.entries[0].envelope.body["live_trace"] == "run-7"


def test_each_entry_carries_its_canonical_digest_and_object_id(repo: Path) -> None:
    ledger = FakeLedger()

    first = _write(repo, _ready(), ledger)
    second = _write(repo, _certificate(), ledger)

    manifest = read_manifest(repo, SUBJECT)
    assert manifest is not None
    assert [e.digest for e in manifest.entries] == [first.digest, second.digest]
    assert manifest.entries[1].predecessor == first.digest
    # The OID is a real object in the repository, not a recomputed hash.
    for entry in manifest.entries:
        blob = subprocess.run(
            ["git", "-C", str(repo), "cat-file", "blob", entry.oid],
            capture_output=True,
            check=True,
        ).stdout
        assert json.loads(blob)["subject"] == SUBJECT


def test_the_write_fast_forwards_rather_than_replacing_history(repo: Path) -> None:
    ledger = FakeLedger()

    first = _write(repo, _ready(), ledger)
    second = _write(repo, _certificate(), ledger)

    parents = subprocess.run(
        ["git", "-C", str(repo), "rev-list", "--first-parent", second.ref_after],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert parents == [second.ref_after, first.ref_after]


def test_a_divergent_write_conflicts_and_never_overwrites(repo: Path) -> None:
    ledger = FakeLedger()
    _write(repo, _ready(), ledger)
    head = ref_head(repo)

    # A second writer that still believes the ref is empty.
    with pytest.raises(CertificateConflict):
        append_certificate(
            repo, _certificate(), ledger=ledger, now=NOW, expected_head=None
        )

    assert ref_head(repo) == head
    manifest = read_manifest(repo, SUBJECT)
    assert manifest is not None and len(manifest.entries) == 1


def test_replaying_an_envelope_already_recorded_is_refused(repo: Path) -> None:
    ledger = FakeLedger()
    envelope = _certificate()
    _write(repo, envelope, ledger)
    head = ref_head(repo)

    with pytest.raises(CertificateRefused, match="already recorded"):
        _write(repo, envelope, ledger)

    assert ref_head(repo) == head


def test_a_stale_predecessor_claim_is_refused_as_a_rewrite(repo: Path) -> None:
    ledger = FakeLedger()
    first = _write(repo, _ready(), ledger)
    _write(repo, _certificate(), ledger)

    with pytest.raises(CertificateRefused, match="predecessor"):
        append_certificate(
            repo,
            _certificate(issued_at="2026-08-24T12:30:00+00:00"),
            ledger=ledger,
            now=NOW,
            predecessor=first.digest,
        )


def test_an_envelope_for_another_subject_never_lands_in_this_manifest(
    repo: Path,
) -> None:
    ledger = FakeLedger()
    _write(repo, _certificate(), ledger)

    with pytest.raises(CertificateRefused, match="subject"):
        append_certificate(
            repo,
            _certificate(subject=OTHER_SUBJECT),
            ledger=ledger,
            now=NOW,
            subject=SUBJECT,
        )


def test_an_unknown_envelope_kind_is_refused(repo: Path) -> None:
    ledger = FakeLedger()

    with pytest.raises(CertificateRefused, match="kind"):
        _write(repo, _certificate(kind="totally_certified"), ledger)


def test_an_unsupported_envelope_version_is_refused(repo: Path) -> None:
    ledger = FakeLedger()

    with pytest.raises(CertificateRefused, match="version"):
        _write(repo, _certificate(version=99), ledger)


def test_a_certificate_for_a_second_subject_leaves_the_first_intact(
    repo: Path,
) -> None:
    ledger = FakeLedger()
    _write(repo, _certificate(), ledger)
    append_certificate(
        repo, _certificate(subject=OTHER_SUBJECT), ledger=ledger, now=NOW
    )

    assert read_manifest(repo, SUBJECT) is not None
    other = read_manifest(repo, OTHER_SUBJECT)
    assert other is not None and other.subject == OTHER_SUBJECT


def test_every_append_is_recorded_in_the_audit_chain(repo: Path) -> None:
    ledger = FakeLedger()

    result = _write(repo, _certificate(), ledger)

    (row,) = ledger.rows
    assert row["event"] == "certificate_appended"
    payload = row["payload"]
    assert payload["subject"] == SUBJECT
    assert payload["kind"] == KIND_CERTIFICATE
    assert payload["status"] == "certified"
    assert payload["digest"] == result.digest
    assert payload["oid"] == result.oid
    assert payload["predecessor"] is None
    assert payload["ref_before"] is None
    assert payload["ref_after"] == result.ref_after
    assert payload["actor"] == "release"
    assert row["timestamp"] == NOW.isoformat()


# --------------------------------------------------------------------------- #
# Resolver: fail-closed in both directions
# --------------------------------------------------------------------------- #


def test_a_readable_ref_with_no_entry_for_this_subject_is_truly_uncertified(
    repo: Path,
) -> None:
    ledger = FakeLedger()
    append_certificate(
        repo, _certificate(subject=OTHER_SUBJECT), ledger=ledger, now=NOW
    )

    reading = _resolve(repo, ledger)

    assert reading.state == "uncertified"
    assert reading.availability == "available"
    assert "no certificate evidence" in reading.detail


def test_an_absent_ref_is_evidence_unavailable_not_uncertified(repo: Path) -> None:
    reading = _resolve(repo, FakeLedger())

    assert reading.state == "unknown"
    assert reading.availability == "unavailable"
    assert reading.certified_at is None


def test_an_unfetched_ref_is_evidence_unavailable(repo: Path) -> None:
    ledger = FakeLedger()
    _write(repo, _certificate(), ledger)

    reading = _resolve(repo, ledger, fetched_at=None)

    assert reading.state == "unknown"
    assert reading.availability == "unavailable"
    assert "fetch" in reading.detail


def test_a_stale_reading_is_never_certified(repo: Path) -> None:
    ledger = FakeLedger()
    _write(repo, _certificate(), ledger)

    reading = _resolve(repo, ledger, fetched_at=NOW - timedelta(days=3))

    assert reading.availability == "stale"
    assert reading.state == "unknown"
    assert "stale" in reading.detail


def test_an_unreadable_repository_is_evidence_unavailable(tmp_path: Path) -> None:
    reading = resolve_certificate(
        tmp_path / "not-a-repo", SUBJECT, fetched_at=NOW, now=NOW
    )

    assert reading.state == "unknown"
    assert reading.availability == "unavailable"


def test_certification_ready_can_never_resolve_to_certified(repo: Path) -> None:
    ledger = FakeLedger()
    _write(repo, _ready(), ledger)

    reading = _resolve(repo, ledger)

    assert reading.state == "uncertified"
    assert reading.availability == "available"
    assert "certification_ready" in reading.detail
    assert "build" in reading.detail


def test_an_anchored_journey_certificate_resolves_to_certified(repo: Path) -> None:
    ledger = FakeLedger()
    _write(repo, _ready(), ledger)
    result = _write(repo, _certificate(), ledger)

    reading = _resolve(repo, ledger)

    assert reading.state == "certified"
    assert reading.availability == "available"
    assert reading.certified_at == "2026-08-24T11:30:00+00:00"
    assert reading.lane == "claude-code/github"
    assert reading.digest == result.digest
    assert reading.evidence_ref is not None
    assert CERTIFICATE_REF in reading.evidence_ref


def test_a_certificate_with_no_audit_binding_is_not_certified(repo: Path) -> None:
    ledger = FakeLedger()
    _write(repo, _certificate(), ledger)

    reading = _resolve(repo, ledger, audit_rows=[])

    assert reading.state == "unknown"
    assert reading.availability == "unavailable"
    assert "audit" in reading.detail


def test_a_certificate_outside_every_current_anchor_is_not_certified(
    repo: Path,
) -> None:
    ledger = FakeLedger()
    _write(repo, _certificate(), ledger)

    reading = _resolve(repo, ledger, anchors=[])

    assert reading.state == "unknown"
    assert reading.availability == "unavailable"
    assert "anchor" in reading.detail


def test_an_anchor_that_predates_the_append_does_not_cover_it(repo: Path) -> None:
    ledger = FakeLedger()
    stale_anchor = Anchor(
        id=0, entry_hash="e" * 64, count=0, timestamp="2026-08-01T00:00:00+00:00"
    )
    _write(repo, _certificate(), ledger)

    reading = _resolve(repo, ledger, anchors=[stale_anchor])

    assert reading.state == "unknown"
    assert "anchor" in reading.detail


def test_a_later_status_envelope_appends_rather_than_replacing(repo: Path) -> None:
    ledger = FakeLedger()
    _write(repo, _certificate(), ledger)
    _write(
        repo,
        CertificateEnvelope(
            kind=KIND_STATUS,
            subject=SUBJECT,
            status="recertification_due",
            lane="claude-code/github",
            fingerprint="fp-2",
            issued_at="2026-08-24T11:45:00+00:00",
            actor="release",
        ),
        ledger,
    )

    manifest = read_manifest(repo, SUBJECT)
    assert manifest is not None
    assert [e.kind for e in manifest.entries] == [KIND_CERTIFICATE, KIND_STATUS]
    assert _resolve(repo, ledger).state == "recertification_due"


def test_a_suspension_envelope_removes_the_certified_claim(repo: Path) -> None:
    ledger = FakeLedger()
    _write(repo, _certificate(), ledger)
    _write(
        repo,
        CertificateEnvelope(
            kind=KIND_STATUS,
            subject=SUBJECT,
            status="suspended",
            lane="claude-code/github",
            fingerprint="fp-1",
            issued_at="2026-08-24T11:50:00+00:00",
            actor="release",
        ),
        ledger,
    )

    reading = _resolve(repo, ledger)

    assert reading.state == "suspended"
    assert reading.certified_at is None


def test_an_unknown_status_value_is_refused_at_write_time(repo: Path) -> None:
    ledger = FakeLedger()

    with pytest.raises(CertificateRefused, match="status"):
        _write(
            repo,
            CertificateEnvelope(
                kind=KIND_STATUS,
                subject=SUBJECT,
                status="definitely_fine",
                lane="claude-code/github",
                fingerprint="fp-1",
                issued_at="2026-08-24T11:50:00+00:00",
                actor="release",
            ),
            ledger,
        )


def test_a_tampered_manifest_chain_fails_closed(repo: Path) -> None:
    ledger = FakeLedger()
    _write(repo, _ready(), ledger)
    _write(repo, _certificate(), ledger)
    manifest = read_manifest(repo, SUBJECT)
    assert manifest is not None
    # Drop the first entry: the survivor's predecessor now names nothing.
    forged = {
        "version": 1,
        "subject": SUBJECT,
        "entries": [manifest.entries[1].to_json()],
    }
    _force_manifest(repo, SUBJECT, forged)

    reading = _resolve(repo, ledger)

    assert reading.state == "unknown"
    assert reading.availability == "unavailable"


def test_a_deleted_ref_reads_as_unavailable_not_as_a_clean_slate(
    repo: Path,
) -> None:
    ledger = FakeLedger()
    _write(repo, _certificate(), ledger)
    subprocess.run(
        ["git", "-C", str(repo), "update-ref", "-d", CERTIFICATE_REF], check=True
    )

    reading = _resolve(repo, ledger)

    assert reading.state == "unknown"
    assert reading.availability == "unavailable"


def _force_manifest(repo: Path, subject: str, document: dict[str, Any]) -> None:
    """Rewrite the manifest blob behind the carrier's back, as an attacker would."""
    index = repo / ".git" / "cert-attack-index"
    env = {"GIT_INDEX_FILE": str(index)}
    head = ref_head(repo)
    assert head is not None
    _git(repo, ["read-tree", head], env)
    oid = _git(
        repo,
        ["hash-object", "-w", "-t", "blob", "--stdin"],
        env,
        stdin=json.dumps(document).encode(),
    )
    _git(repo, ["update-index", "--add", "--cacheinfo", f"100644,{oid},{subject}"], env)
    tree = _git(repo, ["write-tree"], env)
    commit = _git(repo, ["commit-tree", tree, "-p", head, "-m", "attack"], env)
    _git(repo, ["update-ref", CERTIFICATE_REF, commit, head], env)


def _git(
    repo: Path, args: list[str], env: dict[str, str], stdin: bytes | None = None
) -> str:
    import os

    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        input=stdin,
        capture_output=True,
        check=True,
        env={**os.environ, **env},
    )
    return result.stdout.decode().strip()


def test_the_audit_chain_still_verifies_after_a_certificate_write(
    repo: Path, tmp_path: Path
) -> None:
    """The binding is a real chained row, not a note beside the ledger — so
    the whole chain must still verify with it in place (#1612)."""
    store = SqliteRunStore(str(tmp_path / "dispatch.db"))
    try:
        store.append_audit_event(
            timestamp="2026-08-24T10:00:00+00:00", event="dispatch_started"
        )
        result = append_certificate(repo, _certificate(), ledger=store, now=NOW)
        store.append_audit_event(
            timestamp="2026-08-24T13:00:00+00:00", event="dispatch_completed"
        )

        rows = store.read_chain_rows()
        assert verify_chain(rows).ok
        appended = [r for r in rows if r.event == "certificate_appended"]
        assert len(appended) == 1
        payload = json.loads(appended[0].payload or "{}")
        assert payload["digest"] == result.digest
        assert payload["ref_after"] == result.ref_after
        # The resolver reads the SAME rows the verifier just walked.
        reading = resolve_certificate(
            repo,
            SUBJECT,
            audit_rows=rows,
            anchors=[
                Anchor(
                    id=rows[-1].id,
                    entry_hash=entry_hash(rows[-1]),
                    count=len(rows),
                    timestamp=NOW.isoformat(),
                    prev="0" * 64,
                )
            ],
            fetched_at=NOW,
            now=NOW,
        )
        assert reading.state == "certified"
    finally:
        store.close()


def test_the_resolver_fails_closed_on_an_unreadable_tail_before_the_binding() -> None:
    """A tail whose envelope version or status this resolver cannot fully read
    must fail closed at resolve time, never fall through to `certified`.

    Write validation is the primary gate, but re-validating at read time means
    "won't certify" holds even if the audit chain were forged to carry such an
    envelope. The guard runs before the audit-binding check, so an empty ledger
    still proves it: without the guard these would reach `_certified_or_not`.
    """
    from kew.certificate_ref import ENVELOPE_VERSION, Manifest, ManifestEntry, _verdict

    def _one(envelope: CertificateEnvelope) -> Manifest:
        entry = ManifestEntry(
            digest="d" * 64,
            oid="0" * 40,
            predecessor=None,
            recorded_at=NOW.isoformat(),
            envelope=envelope,
        )
        return Manifest(subject=SUBJECT, entries=(entry,))

    for envelope in (
        _certificate(
            version=ENVELOPE_VERSION + 1
        ),  # a version this resolver cannot read
        _certificate(status="totally-bogus"),  # a status not valid for a certificate
    ):
        reading = _verdict(_one(envelope), CERTIFICATE_REF, "head", [], [])
        assert reading.state == "unknown", envelope
        assert reading.availability == "unavailable", envelope
