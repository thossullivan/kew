"""Tests for the audit envelope chokepoint (audit.py)."""

import hashlib
import json
import logging
import multiprocessing
import sqlite3
import threading
from datetime import datetime

import pytest
from support.checkout_generations import (
    fixture_checkout_generation,
    fixture_claim_evidence,
)
from typer.testing import CliRunner

from kew.audit_chain import verify_chain
from kew.ledger_identity import (
    RegistryRecord,
    open_managed_store,
    registry_paths,
)
from kew.run_store import SqliteRunStore

cli_runner = CliRunner()


def _raw_update(db_path, sql, row_id):
    """Edit one stored row through the database file, behind kew's back."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(sql, (row_id,))
        conn.commit()
    finally:
        conn.close()


def _legacy_hash(row):
    """The twelve-slot entry hash every row had before the format step."""
    slots = [
        row["id"],
        row["timestamp"],
        row["event"],
        row["run_id"],
        row["issue_num"],
        row["repo"],
        row["actor_type"],
        row["actor_id"],
        row["endpoint"],
        row["policy_hash"],
        row["payload"],
        row["prev_hash"],
    ]
    canonical = json.dumps(slots, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _write_schema_76_ledger(db_path):
    """A ledger as a schema-76 kew left it: no chain_format column, every row
    hashed in the twelve-slot form, two of them carrying item_id/dedup_key.

    Built from today's schema with the v77 column dropped by raw sqlite3, so
    the remaining tables match v76 exactly.
    """
    SqliteRunStore(str(db_path)).close()
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("DROP TRIGGER audit_chain_format_no_downgrade")
        conn.execute("ALTER TABLE audit_events DROP COLUMN chain_format")
        conn.execute("UPDATE schema_version SET version = 76 WHERE id = 1")
        for item_id, dedup_key in (("SFD-1", None), ("7", "publication:7")):
            tail = conn.execute(
                "SELECT * FROM audit_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
            prev = _legacy_hash(tail) if tail is not None else "0" * 64
            conn.execute(
                "INSERT INTO audit_events (timestamp, event, actor_type, actor_id,"
                " prev_hash, item_id, dedup_key) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    "2026-10-01T00:00:00+00:00",
                    "epic_merge",
                    "human",
                    "tom",
                    prev,
                    item_id,
                    dedup_key,
                ),
            )
        conn.commit()
        rows = conn.execute("SELECT * FROM audit_events ORDER BY id").fetchall()
        return {row["id"]: _legacy_hash(row) for row in rows}
    finally:
        conn.close()


def test_ledger_written_before_the_format_step_still_verifies_after_migration(
    tmp_path,
):
    from kew.audit import Actor, emit
    from kew.audit_chain import entry_hash

    db = tmp_path / "dispatch.db"
    old_hashes = _write_schema_76_ledger(db)
    with SqliteRunStore(str(db)) as store:
        version = store._conn.execute("SELECT version FROM schema_version")
        assert version.fetchone()[0] >= 77
        emit(store, "epic_merge", actor=Actor("human", "a"), item_id="SFD-2")
        rows = store.read_chain_rows()
    assert verify_chain(rows).ok
    assert {r.id: entry_hash(r) for r in rows[:-1]} == old_hashes
    assert [r.chain_format for r in rows] == [1] * len(old_hashes) + [2]
    assert rows[-1].item_id == "SFD-2"


def _stale_writer_append(db_path, event):
    """Append the way a kew opened before the v77 step does: the twelve-slot
    hash of the tail, and an insert that never names chain_format."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN IMMEDIATE")
        tail = conn.execute(
            "SELECT * FROM audit_events ORDER BY id DESC LIMIT 1"
        ).fetchone()
        prev = _legacy_hash(tail) if tail is not None else "0" * 64
        conn.execute(
            "INSERT INTO audit_events (timestamp, event, actor_type, actor_id,"
            " prev_hash) VALUES (?, ?, ?, ?, ?)",
            ("2026-10-02T00:00:00+00:00", event, "human", "tom", prev),
        )
        conn.commit()
    finally:
        conn.close()


def test_a_stale_writer_cannot_append_after_a_format_two_row(tmp_path):
    from kew.audit import Actor, emit

    db = tmp_path / "dispatch.db"
    _write_schema_76_ledger(db)
    with SqliteRunStore(str(db)) as store:
        emit(store, "epic_merge", actor=Actor("human", "a"), item_id="SFD-2")
    with pytest.raises(sqlite3.IntegrityError, match="chain format"):
        _stale_writer_append(db, "stale")
    with SqliteRunStore(str(db)) as store:
        rows = store.read_chain_rows()
    assert verify_chain(rows).ok
    assert [r.event for r in rows][-1] == "epic_merge"


def test_a_stale_writer_still_appends_before_the_first_format_two_row(tmp_path):
    from kew.audit import Actor, emit

    db = tmp_path / "dispatch.db"
    _write_schema_76_ledger(db)
    SqliteRunStore(str(db)).close()
    _stale_writer_append(db, "stale")
    with SqliteRunStore(str(db)) as store:
        emit(store, "epic_merge", actor=Actor("human", "a"), item_id="SFD-2")
        rows = store.read_chain_rows()
    assert verify_chain(rows).ok
    assert [(r.event, r.chain_format) for r in rows[-2:]] == [
        ("stale", 1),
        ("epic_merge", 2),
    ]


def test_a_write_probe_passes_and_leaves_nothing_on_a_format_two_ledger(tmp_path):
    from kew.audit import Actor, emit

    with SqliteRunStore(str(tmp_path / "dispatch.db")) as store:
        emit(store, "epic_merge", actor=Actor("human", "a"), item_id="SFD-1")
        before = store.read_chain_rows()
        store.probe_audit_write()
        assert store.read_chain_rows() == before
        emit(store, "epic_merge", actor=Actor("human", "a"), item_id="SFD-2")
        rows = store.read_chain_rows()
    assert verify_chain(rows).ok
    assert [r.chain_format for r in rows] == [2] * len(rows)


def test_a_write_probe_raises_when_the_ledger_refuses_an_audit_row(tmp_path):
    db = tmp_path / "dispatch.db"
    SqliteRunStore(str(db)).close()
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "CREATE TRIGGER refuse_audit BEFORE INSERT ON audit_events"
            " BEGIN SELECT RAISE(ABORT, 'ledger refuses audit rows'); END"
        )
        conn.commit()
    finally:
        conn.close()
    with (
        SqliteRunStore(str(db)) as store,
        pytest.raises(sqlite3.IntegrityError, match="refuses audit rows"),
    ):
        store.probe_audit_write()


class _TakesOverOneHold:
    """A host that takes one dead holder's lease over, writing it pending."""

    name = "gcp"
    address = "verify-1"
    resource_key = "gcp/proj/us-central1-a/verify-1"
    takes_over_holds = True

    def record_takeovers_in(self, ledger):
        ledger.pending("dead-holder", "takeover-1")


def test_a_takeover_is_recorded_on_a_ledger_holding_a_format_two_row(tmp_path):
    from kew.audit import Actor, emit
    from kew.config import DispatchConfig
    from kew.host_takeovers import LEASE_TAKEOVER_PENDING_EVENT, record_lapsed_leases

    with SqliteRunStore(str(tmp_path / "dispatch.db")) as store:
        emit(store, "dispatch_completed", actor=Actor("host", "kew"), item_id="7")
        record_lapsed_leases(store, [_TakesOverOneHold()], config=DispatchConfig())
        pending = store.list_audit_events(event=LEASE_TAKEOVER_PENDING_EVENT)
        rows = store.read_chain_rows()
    assert [e.payload["takeover_id"] for e in pending] == ["takeover-1"]
    assert verify_chain(rows).ok
    assert rows[-1].chain_format == 2


def test_audit_export_missing_database_exits_two(tmp_path, monkeypatch):
    from kew.cli import app

    monkeypatch.chdir(tmp_path)
    result = cli_runner.invoke(app, ["audit", "export"])
    assert result.exit_code == 2, result.output
    assert "No dispatch database found" in result.output


@pytest.fixture(autouse=True)
def _reset_audit_context():
    """Each test starts with no module-level repo default."""
    import kew.audit as audit

    audit.set_audit_context(repo=None)
    yield
    audit.set_audit_context(repo=None)


@pytest.fixture(autouse=True)
def _managed_audit_project(tmp_path, monkeypatch):
    """Give audit CLI tests an isolated canonical project/registry root."""
    (tmp_path / ".git").mkdir(exist_ok=True)
    project = tmp_path / "project-root"
    project.mkdir()
    (project / ".git").mkdir()
    monkeypatch.setattr("kew.audit.project_root", lambda: project)


def test_sidecar_evidence_contains_ids_and_no_credentials(tmp_path):
    from dataclasses import replace

    import kew.audit as audit
    import kew.readiness as readiness

    payload_builder = getattr(audit, "sidecar_evidence", None)
    evidence_type = getattr(readiness, "SidecarTopologyEvidence", None)
    packet_type = getattr(readiness, "SidecarPacketEvidence", None)
    outcome_type = getattr(readiness, "TopologyProbeResult", None)
    packet_status = getattr(readiness, "PacketProbeStatus", None)

    assert payload_builder is not None
    assert evidence_type is not None
    assert packet_type is not None
    assert outcome_type is not None
    assert packet_status is not None

    packet = packet_type(
        proxy_health=packet_status.REACHABLE,
        proxy_route=packet_status.REACHABLE,
        denials=tuple(
            outcome_type(
                name=name,
                positive_control=packet_status.REACHABLE,
                worker=packet_status.DENIED,
                passed=True,
            )
            for name in readiness.SIDECAR_PACKET_DENIALS
        ),
    )
    evidence = evidence_type(
        schema_version=1,
        stage="admission",
        run_id="run-sidecar",
        attempt_number=1,
        profile_fingerprint="a" * 64,
        route="synthetic/route",
        config_sha256="b" * 64,
        owner="c" * 32,
        worker_image_id="sha256:" + "d" * 64,
        proxy_image_id="sha256:" + "e" * 64,
        network_id="f" * 64,
        egress_network_id="1" * 64,
        proxy_id="2" * 64,
        worker_id="3" * 64,
        config_helper_id="4" * 64,
        filter_helper_id="5" * 64,
        config_storage="kew-switchyard-synthetic-config",
        endpoint="http://192.0.2.10:4000",
        observed_version="0.1.0",
        packet=packet,
        filter_verdict="passed",
        cleanup_verdict="pending",
    )
    secret = "synthetic-provider-credential"
    payload = payload_builder(evidence)

    with SqliteRunStore(str(tmp_path / "sidecar.db")) as store:
        audit.append_sidecar_evidence(
            store,
            evidence,
            actor=audit.Actor("policy", "sidecar-supervisor"),
            issue_num=7,
            item_id="7",
        )
        event = store.list_audit_events(event=audit.SIDECAR_EVIDENCE_EVENT)[0]
        audit.append_sidecar_evidence(
            store,
            replace(evidence, stage="terminal", cleanup_verdict="passed"),
            actor=audit.Actor("policy", "sidecar-supervisor"),
        )
        with pytest.raises(audit.SidecarPacketEvidenceUnavailable):
            audit.matching_sidecar_packet_evidence(
                store,
                worker_image_id=evidence.worker_image_id,
                proxy_image_id=evidence.proxy_image_id,
            )

    serialized = json.dumps(event.payload)
    assert event.payload == payload
    assert event.payload["worker_id"] == "3" * 64
    assert event.payload["proxy_id"] == "2" * 64
    assert event.payload["network_id"] == "f" * 64
    assert secret not in serialized
    assert "environment" not in serialized
    assert "raw" not in serialized


# VOLUME is deliberately not a leg: the evidence window is read in pages, so a
# pile of valid certifications is proof and not an overflow. The two bounds
# that remain have their own tests below.
@pytest.mark.parametrize(
    "invalid",
    [
        "conflict",
        "stale",
        "future",
        "scope",
        "malformed",
        "owner",
        "images",
        "missing",
    ],
)
def test_conflicting_sidecar_packet_certification_refuses_earlier_pass(
    tmp_path, invalid
):
    from dataclasses import replace
    from datetime import UTC, timedelta

    from kew.audit import (
        SIDECAR_PACKET_CERTIFICATION_EVENT,
        Actor,
        SidecarPacketEvidenceUnavailable,
        append_sidecar_packet_certification,
        matching_sidecar_packet_evidence,
    )
    from kew.readiness import (
        SIDECAR_PACKET_DENIALS,
        PacketProbeStatus,
        SidecarPacketCertification,
        SidecarPacketEvidence,
        evaluate_topology_denial,
    )

    packet = SidecarPacketEvidence(
        proxy_health=PacketProbeStatus.REACHABLE,
        proxy_route=PacketProbeStatus.REACHABLE,
        denials=tuple(
            evaluate_topology_denial(
                name=name,
                positive_control=PacketProbeStatus.REACHABLE,
                worker=PacketProbeStatus.DENIED,
            )
            for name in SIDECAR_PACKET_DENIALS
        ),
    )
    certification = SidecarPacketCertification(
        schema_version=1,
        worker_image_id="sha256:" + "a" * 64,
        proxy_image_id="sha256:" + "b" * 64,
        packet=packet,
    )
    failed = replace(
        certification,
        packet=replace(packet, proxy_health=PacketProbeStatus.DENIED),
    )
    with SqliteRunStore(str(tmp_path / "packet-proof.db")) as store:
        if invalid == "conflict":
            append_sidecar_packet_certification(
                store, certification, actor=Actor("policy", "sidecar-packet-gate")
            )
        timestamp = datetime.now(UTC)
        payload = failed.to_dict() if invalid == "conflict" else certification.to_dict()
        if invalid == "stale":
            timestamp -= timedelta(hours=25)
        if invalid == "future":
            timestamp += timedelta(minutes=1)
        if invalid == "scope":
            payload["filter_policy_sha256"] = "f" * 64
        if invalid == "malformed":
            payload["unexpected"] = "field"
        if invalid == "images":
            payload["worker_image_id"] = "sha256:" + "c" * 64
        if invalid != "missing":
            store.append_audit_event(
                timestamp=timestamp.isoformat(),
                event=SIDECAR_PACKET_CERTIFICATION_EVENT,
                actor_type="policy",
                actor_id="wrong-owner" if invalid == "owner" else "sidecar-packet-gate",
                payload=payload,
            )
        with pytest.raises(SidecarPacketEvidenceUnavailable):
            matching_sidecar_packet_evidence(
                store,
                worker_image_id=certification.worker_image_id,
                proxy_image_id=certification.proxy_image_id,
            )


def test_a_window_full_of_valid_packet_proof_is_proof_not_an_overflow(tmp_path):
    """Valid evidence must never accumulate into a refusal (#2389 review).

    The lookup used to read one page of 65 and refuse when it filled, so a
    window holding that many good certifications refused every sidecar
    admission — and the remedy for the refusal was to observe one more. Shared
    with the runtime egress matcher, whose production writer made the same
    ceiling reachable from ordinary use.
    """
    from kew.audit import (
        _EVIDENCE_PAGE,
        SIDECAR_PACKET_CERTIFICATION_EVENT,
        Actor,
        append_sidecar_packet_certification,
        matching_sidecar_packet_evidence,
    )
    from kew.readiness import (
        SIDECAR_PACKET_DENIALS,
        PacketProbeStatus,
        SidecarPacketCertification,
        SidecarPacketEvidence,
        evaluate_topology_denial,
    )

    packet = SidecarPacketEvidence(
        proxy_health=PacketProbeStatus.REACHABLE,
        proxy_route=PacketProbeStatus.REACHABLE,
        denials=tuple(
            evaluate_topology_denial(
                name=name,
                positive_control=PacketProbeStatus.REACHABLE,
                worker=PacketProbeStatus.DENIED,
            )
            for name in SIDECAR_PACKET_DENIALS
        ),
    )
    certification = SidecarPacketCertification(
        schema_version=1,
        worker_image_id="sha256:" + "a" * 64,
        proxy_image_id="sha256:" + "b" * 64,
        packet=packet,
    )
    with SqliteRunStore(str(tmp_path / "packet-volume.db")) as store:
        for _ in range(_EVIDENCE_PAGE * 2):
            append_sidecar_packet_certification(
                store, certification, actor=Actor("policy", "sidecar-packet-gate")
            )
        # Non-vacuous: more than one page really has to be on the chain.
        assert (
            len(
                store.list_audit_events(
                    event=SIDECAR_PACKET_CERTIFICATION_EVENT, limit=None
                )
            )
            > _EVIDENCE_PAGE
        )
        assert (
            matching_sidecar_packet_evidence(
                store,
                worker_image_id=certification.worker_image_id,
                proxy_image_id=certification.proxy_image_id,
            )
            == packet
        )


@pytest.mark.parametrize("historical_count", [1, 65])
def test_identical_packet_rerun_renews_expired_observation(
    tmp_path, monkeypatch, historical_count
):
    from datetime import UTC, timedelta

    import kew.audit as audit
    from kew.readiness import (
        SIDECAR_PACKET_DENIALS,
        PacketProbeStatus,
        SidecarPacketCertification,
        SidecarPacketEvidence,
        evaluate_topology_denial,
    )

    packet = SidecarPacketEvidence(
        proxy_health=PacketProbeStatus.REACHABLE,
        proxy_route=PacketProbeStatus.REACHABLE,
        denials=tuple(
            evaluate_topology_denial(
                name=name,
                positive_control=PacketProbeStatus.REACHABLE,
                worker=PacketProbeStatus.DENIED,
            )
            for name in SIDECAR_PACKET_DENIALS
        ),
    )
    certification = SidecarPacketCertification(
        schema_version=1,
        worker_image_id="sha256:" + "a" * 64,
        proxy_image_id="sha256:" + "b" * 64,
        packet=packet,
    )
    observed_at = datetime.now(UTC) - timedelta(hours=25)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return observed_at

    monkeypatch.setattr(audit, "datetime", Clock)
    images = dict(
        worker_image_id=certification.worker_image_id,
        proxy_image_id=certification.proxy_image_id,
    )
    with SqliteRunStore(str(tmp_path / "packet-proof.db")) as store:
        for _ in range(historical_count):
            audit.append_sidecar_packet_certification(
                store, certification, actor=audit.Actor("policy", "sidecar-packet-gate")
            )
            observed_at += timedelta(microseconds=1)
        prior = store.list_audit_events(
            event=audit.SIDECAR_PACKET_CERTIFICATION_EVENT, limit=None
        )
        observed_at += timedelta(hours=25)
        with pytest.raises(audit.SidecarPacketEvidenceUnavailable):
            audit.matching_sidecar_packet_evidence(store, **images)
        audit.append_sidecar_packet_certification(
            store, certification, actor=audit.Actor("policy", "sidecar-packet-gate")
        )
        try:
            renewed = audit.matching_sidecar_packet_evidence(store, **images)
        except audit.SidecarPacketEvidenceUnavailable:
            renewed = None
        assert renewed == packet, "a real rerun must replace expired eligibility"
        events = store.list_audit_events(
            event=audit.SIDECAR_PACKET_CERTIFICATION_EVENT, limit=None
        )
        assert len(events) == historical_count + 1
        assert events[0].timestamp == observed_at.isoformat()
        assert events[1:] == prior
        assert verify_chain(store.read_chain_rows()).ok


class TestActorResolution:
    """Actor matrix: default OS user, KEW_ACTOR, empty KEW_ACTOR, explicit."""

    def test_default_resolves_os_user(self, monkeypatch):
        from kew.audit import resolve_actor

        monkeypatch.delenv("KEW_ACTOR", raising=False)
        monkeypatch.setattr("kew.audit.getpass.getuser", lambda: "osuser")
        actor = resolve_actor()
        assert actor.type == "human"
        assert actor.id == "osuser"

    def test_kew_actor_overrides(self, monkeypatch):
        from kew.audit import resolve_actor

        monkeypatch.setenv("KEW_ACTOR", "alice")
        actor = resolve_actor()
        assert actor.type == "human"
        assert actor.id == "alice"

    def test_empty_kew_actor_treated_as_unset(self, monkeypatch):
        from kew.audit import resolve_actor

        monkeypatch.setenv("KEW_ACTOR", "   ")
        monkeypatch.setattr("kew.audit.getpass.getuser", lambda: "osuser")
        assert resolve_actor().id == "osuser"

    @pytest.mark.parametrize(
        "exc",
        [
            OSError("no passwd entry"),
            KeyError("uid not in passwd"),
            ImportError("no pwd module"),
        ],
    )
    def test_getuser_failure_falls_back_to_unknown(self, monkeypatch, exc):
        from kew.audit import resolve_actor

        monkeypatch.delenv("KEW_ACTOR", raising=False)

        def _boom():
            raise exc

        monkeypatch.setattr("kew.audit.getpass.getuser", _boom)
        assert resolve_actor().id == "unknown"

    def test_explicit_policy_actor(self):
        from kew.audit import Actor

        actor = Actor("policy", "budget-breach")
        assert actor.type == "policy"
        assert actor.id == "budget-breach"

    def test_explicit_agent_actor(self):
        from kew.audit import Actor

        actor = Actor("agent", "claude-code")
        assert actor.type == "agent"


class TestEmit:
    """emit() writes through the store; best-effort; envelope on every row."""

    def test_emit_writes_full_envelope(self, tmp_path):
        from kew.audit import Actor, emit

        with SqliteRunStore(str(tmp_path / "t.db")) as store:
            emit(
                store,
                "dispatch_completed",
                actor=Actor("human", "alice"),
                run_id="run-1",
                issue_num=42,
                endpoint="anthropic:default",
                repo="octo/widgets",
                payload={"exit_code": 0},
            )
            ev = store.list_audit_events()[0]
            assert ev.event == "dispatch_completed"
            assert ev.run_id == "run-1"
            assert ev.issue_num == 42
            assert ev.actor_type == "human"
            assert ev.actor_id == "alice"
            assert ev.endpoint == "anthropic:default"
            assert ev.repo == "octo/widgets"
            assert ev.payload == {"exit_code": 0}
            assert ev.policy_hash is None
            assert ev.prev_hash == "0" * 64  # GENESIS_PREV for first row (#249)
            # timestamp is ISO 8601 UTC, parseable
            parsed = datetime.fromisoformat(ev.timestamp)
            assert parsed.tzinfo is not None

    def test_transport_evidence_is_sanitized_and_secret_free(self, tmp_path):
        from kew.audit import Actor, emit, transport_evidence

        raw_endpoint = "https://user:token@switchyard.example:8443/private?key=x"
        config_contents = "provider_api_key = 'do-not-persist'"
        evidence = transport_evidence(
            mode="switchyard",
            endpoint=raw_endpoint,
            route="switchyard/passthrough",
            managed=False,
            expected_version="6ff491a",
            observed_version="6ff491a",
            config_sha256="b" * 64,
            preflight_verdict="ready",
            binary_sha256="c" * 64,
            credential_env_names=("OPENAI_API_KEY",),
        )
        with SqliteRunStore(str(tmp_path / "t.db")) as store:
            emit(
                store,
                "transport_admitted",
                actor=Actor("policy", "transport-preflight"),
                payload=evidence,
            )
            event = store.list_audit_events()[0]

        assert event.payload == {
            "transport_mode": "switchyard",
            "transport_endpoint": "https://switchyard.example:8443",
            "transport_route": "switchyard/passthrough",
            "transport_managed": False,
            "transport_expected_version": "6ff491a",
            "transport_observed_version": "6ff491a",
            "transport_config_sha256": "b" * 64,
            "transport_preflight_verdict": "ready",
            "transport_binary_sha256": "c" * 64,
            "transport_credential_env_names": ["OPENAI_API_KEY"],
        }
        serialized = json.dumps(event.payload)
        assert "token" not in serialized
        assert config_contents not in serialized

    def test_continuation_evidence_is_sanitized_and_chainable(self, tmp_path):
        from kew.audit import Actor, continuation_evidence, emit

        raw_worktree = "/Users/alice/private/recovery worktree\nwith-secrets"
        evidence = continuation_evidence(
            phase="detection",
            epic_ref="epic/recovery",
            item_id="SFD-174",
            original_run_id="original-run",
            continuation_run_id=None,
            branch="agent/issue-SFD-174",
            worktree_identity=raw_worktree,
            state="awaiting_continuation",
            outcome="crash_with_dirty_work",
        )

        with SqliteRunStore(str(tmp_path / "t.db")) as store:
            emit(
                store,
                "continuation_detected",
                actor=Actor("policy", "driver_reconcile"),
                item_id="SFD-174",
                payload=evidence,
            )
            event = store.list_audit_events()[0]
            assert verify_chain(store.read_chain_rows()).ok

        assert event.payload["recovery_phase"] == "detection"
        assert event.payload["original_run_id"] == "original-run"
        assert event.payload["continuation_run_id"] is None
        assert event.payload["branch"] == "agent/issue-SFD-174"
        assert event.payload["worktree_identity"].startswith("sha256:")
        assert raw_worktree not in json.dumps(event.payload)

    def test_emit_binds_continuation_lineage_to_publication_and_terminal_events(
        self, tmp_path
    ):
        from kew.audit import Actor, emit

        worktree = tmp_path / "preserved"
        with SqliteRunStore(str(tmp_path / "t.db")) as store:
            record = store.record_continuation(
                "epic/recovery",
                "SFD-174",
                "original-run",
                "agent/issue-SFD-174",
                worktree,
                checkout_generation=fixture_checkout_generation(worktree),
            )
            assert (
                store.claim_continuation_under_cap(
                    record.epic_ref,
                    record.item_id,
                    record.original_run_id,
                    record.branch,
                    record.worktree_identity,
                    "continuation-run",
                    5,
                    claim_evidence=fixture_claim_evidence(
                        store,
                        record.epic_ref,
                        record.item_id,
                        record.original_run_id,
                        record.branch,
                        record.worktree_identity,
                        "continuation-run",
                    ),
                )
                == "claimed"
            )

            for event_name in ("publication_deferred", "dispatch_completed"):
                emit(
                    store,
                    event_name,
                    actor=Actor("agent", "codex"),
                    run_id="continuation-run",
                    item_id="SFD-174",
                    payload={"exit_code": 1},
                )

            events = store.list_audit_events(limit=None)
            assert verify_chain(store.read_chain_rows()).ok

        [binding] = [
            event for event in events if event.event == "continuation_checkout_bound"
        ]
        assert binding.payload["worktree_identity"] == str(worktree)
        assert binding.payload["checkout_id"] == ""
        events = [
            event for event in events if event.event != "continuation_checkout_bound"
        ]
        assert len(events) == 3
        assert {event.event for event in events} == {
            "continuation_claimed",
            "publication_deferred",
            "dispatch_completed",
        }
        for event in events:
            assert event.payload["original_run_id"] == "original-run"
            assert event.payload["continuation_run_id"] == "continuation-run"
            assert event.payload["item_id"] == "SFD-174"
            assert event.payload["branch"] == "agent/issue-SFD-174"
            assert event.payload["worktree_identity"].startswith("sha256:")

    def test_runless_dispatch_event_resolves_claimed_continuation(self, tmp_path):
        from kew.audit import Actor, emit

        worktree = tmp_path / "preserved"
        with SqliteRunStore(str(tmp_path / "t.db")) as store:
            record = store.record_continuation(
                "epic/recovery",
                "SFD-174",
                "original-run",
                "agent/issue-SFD-174",
                worktree,
                checkout_generation=fixture_checkout_generation(worktree),
            )
            assert (
                store.claim_continuation_under_cap(
                    record.epic_ref,
                    record.item_id,
                    record.original_run_id,
                    record.branch,
                    record.worktree_identity,
                    "continuation-run",
                    5,
                    claim_evidence=fixture_claim_evidence(
                        store,
                        record.epic_ref,
                        record.item_id,
                        record.original_run_id,
                        record.branch,
                        record.worktree_identity,
                        "continuation-run",
                    ),
                )
                == "claimed"
            )

            emit(
                store,
                "dispatch_completed",
                actor=Actor("policy", "preflight"),
                run_id=None,
                item_id="SFD-174",
                payload={"epic_ref": "epic/recovery", "exit_code": 1},
            )

            [event] = store.list_audit_events(event="dispatch_completed")
            assert event.run_id == "continuation-run"
            assert event.payload["original_run_id"] == "original-run"
            assert event.payload["continuation_run_id"] == "continuation-run"
            assert event.payload["branch"] == "agent/issue-SFD-174"
            assert event.payload["worktree_identity"].startswith("sha256:")

    def test_recovery_dedupe_keeps_distinct_branch_and_worktree_identities(
        self, tmp_path
    ):
        from kew.audit import Actor, emit_continuation_evidence

        with SqliteRunStore(str(tmp_path / "t.db")) as store:
            first = store.record_continuation(
                "epic/recovery",
                "SFD-174",
                "original-run",
                "agent/issue-SFD-174-a",
                tmp_path / "preserved-a",
                checkout_generation=fixture_checkout_generation(
                    tmp_path / "preserved-a"
                ),
            )
            second = store.record_continuation(
                "epic/recovery",
                "SFD-174",
                "original-run",
                "agent/issue-SFD-174-b",
                tmp_path / "preserved-b",
                checkout_generation=fixture_checkout_generation(
                    tmp_path / "preserved-b"
                ),
            )

            for record in (first, second):
                emit_continuation_evidence(
                    store,
                    "continuation_detected",
                    actor=Actor("policy", "driver_reconcile"),
                    phase="detection",
                    epic_ref=record.epic_ref,
                    item_id=record.item_id,
                    original_run_id=record.original_run_id,
                    continuation_run_id=None,
                    branch=record.branch,
                    worktree_identity=record.worktree_identity,
                    generation=record.generation,
                    state=record.state,
                    outcome="crash_with_dirty_work",
                )

            events = store.list_audit_events(event="continuation_detected")
            assert len(events) == 2
            assert {event.payload["branch"] for event in events} == {
                first.branch,
                second.branch,
            }
            assert len({event.payload["worktree_identity"] for event in events}) == 2

    def test_recovery_dedupe_is_atomic_across_concurrent_writers(
        self, tmp_path, monkeypatch
    ):
        from kew.audit import Actor, emit_continuation_evidence

        db_path = str(tmp_path / "t.db")
        with SqliteRunStore(db_path):
            pass

        original_list = SqliteRunStore.list_audit_events
        absent_read_barrier = threading.Barrier(2)
        start_barrier = threading.Barrier(2)
        errors: list[Exception] = []

        def interleave_absent_reads(store, *args, **kwargs):
            events = original_list(store, *args, **kwargs)
            if threading.current_thread().name.startswith("continuation-writer"):
                absent_read_barrier.wait(timeout=5)
            return events

        monkeypatch.setattr(
            SqliteRunStore, "list_audit_events", interleave_absent_reads
        )

        def writer() -> None:
            try:
                with SqliteRunStore(db_path) as store:
                    start_barrier.wait(timeout=5)
                    emit_continuation_evidence(
                        store,
                        "continuation_detected",
                        actor=Actor("policy", "driver_reconcile"),
                        phase="detection",
                        epic_ref="epic/recovery",
                        item_id="SFD-174",
                        original_run_id="original-run",
                        continuation_run_id=None,
                        branch="agent/issue-SFD-174",
                        worktree_identity=tmp_path / "preserved",
                        generation=0,
                        state="awaiting_continuation",
                        outcome="crash_with_dirty_work",
                    )
            except Exception as exc:  # noqa: BLE001 - retain worker failures
                errors.append(exc)

        writers = [
            threading.Thread(target=writer, name=f"continuation-writer-{index}")
            for index in range(2)
        ]
        for thread in writers:
            thread.start()
        for thread in writers:
            thread.join(timeout=10)

        assert all(not thread.is_alive() for thread in writers)
        assert errors == []
        with SqliteRunStore(db_path) as store:
            events = original_list(store, event="continuation_detected", limit=None)
            assert len(events) == 1
            assert events[0].payload["evidence_identity"] == events[0].dedup_key
            assert verify_chain(store.read_chain_rows()).ok

    def test_repo_defaults_from_audit_context(self, tmp_path):
        from kew.audit import Actor, emit, set_audit_context

        set_audit_context(repo="ctx/repo")
        with SqliteRunStore(str(tmp_path / "t.db")) as store:
            emit(store, "fleet_resume", actor=Actor("human", "a"))
            assert store.list_audit_events()[0].repo == "ctx/repo"

    def test_explicit_repo_beats_context(self, tmp_path):
        from kew.audit import Actor, emit, set_audit_context

        set_audit_context(repo="ctx/repo")
        with SqliteRunStore(str(tmp_path / "t.db")) as store:
            emit(store, "fleet_resume", actor=Actor("human", "a"), repo="x/y")
            assert store.list_audit_events()[0].repo == "x/y"

    def test_no_context_no_repo_is_null(self, tmp_path):
        from kew.audit import Actor, emit

        with SqliteRunStore(str(tmp_path / "t.db")) as store:
            emit(store, "fleet_resume", actor=Actor("human", "a"))
            assert store.list_audit_events()[0].repo is None

    def test_emit_threads_item_id_to_column(self, tmp_path):
        from kew.audit import Actor, emit

        with SqliteRunStore(str(tmp_path / "t.db")) as store:
            emit(
                store,
                "epic_merge",
                actor=Actor("human", "alice"),
                issue_num=0,
                item_id="SFD-12",
                repo="octo/widgets",
            )
            ev = store.list_audit_events()[0]
            assert ev.item_id == "SFD-12"
            assert ev.issue_num == 0

    def test_emit_item_id_defaults_null(self, tmp_path):
        from kew.audit import Actor, emit

        with SqliteRunStore(str(tmp_path / "t.db")) as store:
            emit(store, "fleet_resume", actor=Actor("human", "a"))
            assert store.list_audit_events()[0].item_id is None

    def test_editing_item_id_on_a_stored_event_breaks_verify(self, tmp_path):
        """item_id is in the hash of every row written after the format step:
        changing it by hand leaves the next row's link dangling."""
        from kew.audit import Actor, emit

        db = tmp_path / "t.db"
        with SqliteRunStore(str(db)) as store:
            emit(store, "epic_merge", actor=Actor("human", "a"), item_id="SFD-1")
            emit(store, "epic_merge", actor=Actor("human", "b"), item_id="SFD-2")
            rows = store.read_chain_rows()
            edited, following = rows[-2].id, rows[-1].id
            assert verify_chain(rows).ok
        _raw_update(
            db, "UPDATE audit_events SET item_id = 'SFD-9' WHERE id = ?", edited
        )
        with SqliteRunStore(str(db)) as store:
            result = verify_chain(store.read_chain_rows())
        assert not result.ok
        assert result.broken_at_id == following

    def test_editing_dedup_key_on_a_stored_event_breaks_verify(self, tmp_path):
        db = tmp_path / "t.db"
        with SqliteRunStore(str(db)) as store:
            edited = store.append_audit_event_once(
                dedup_key="publication:7",
                timestamp="2026-10-29T00:00:00+00:00",
                event="publication_intent",
                item_id="7",
                actor_type="host",
                actor_id="kew",
            )
            following = store.append_audit_event(
                timestamp="2026-10-29T00:00:01+00:00",
                event="dispatch_started",
                actor_type="host",
                actor_id="kew",
            )
            assert verify_chain(store.read_chain_rows()).ok
        _raw_update(
            db,
            "UPDATE audit_events SET dedup_key = 'publication:8' WHERE id = ?",
            edited,
        )
        with SqliteRunStore(str(db)) as store:
            result = verify_chain(store.read_chain_rows())
        assert not result.ok
        assert result.broken_at_id == following

    def test_list_audit_events_filters_by_item_id(self, tmp_path):
        from kew.audit import Actor, emit

        with SqliteRunStore(str(tmp_path / "t.db")) as store:
            emit(store, "epic_merge", actor=Actor("human", "a"), item_id="SFD-1")
            emit(store, "epic_merge", actor=Actor("human", "b"), item_id="SFD-2")
            only = store.list_audit_events(item_id="SFD-2")
            assert [e.item_id for e in only] == ["SFD-2"]

    def test_emit_with_no_store_warns_never_raises(self, caplog):
        from kew.audit import Actor, emit

        with caplog.at_level(logging.WARNING, logger="kew.audit"):
            emit(None, "ci_fix_completed", actor=Actor("human", "a"))
        assert any("ci_fix_completed" in r.message for r in caplog.records)

    def test_emit_noops_on_store_without_audit_support(self):
        from kew.audit import Actor, emit

        class LegacyFakeStore:  # no append_audit_event — old protocol shape
            pass

        emit(LegacyFakeStore(), "fleet_stop", actor=Actor("human", "a"))

    def test_emit_swallows_store_failure_with_warning(self, caplog):
        from kew.audit import Actor, emit

        class ExplodingStore:
            def append_audit_event(self, **kwargs):
                raise RuntimeError("disk full")

        with caplog.at_level(logging.WARNING, logger="kew.audit"):
            emit(ExplodingStore(), "fleet_stop", actor=Actor("human", "a"))
        assert any("fleet_stop" in r.message for r in caplog.records)


class TestConfiguredEndpoint:
    """Per-runner configured-endpoint capture, env set and unset."""

    def test_claude_code_default(self, monkeypatch):
        from kew.audit import configured_endpoint

        monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
        assert (
            configured_endpoint("claude-code", "claude-sonnet-4-6")
            == "anthropic:default"
        )

    def test_claude_code_base_url_set(self, monkeypatch):
        from kew.audit import configured_endpoint

        monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://proxy.corp:8443")
        assert (
            configured_endpoint("claude-code", "claude-sonnet-4-6")
            == "https://proxy.corp:8443"
        )

    def test_claude_code_empty_base_url_treated_as_unset(self, monkeypatch):
        from kew.audit import configured_endpoint

        monkeypatch.setenv("ANTHROPIC_BASE_URL", "  ")
        assert (
            configured_endpoint("claude-code", "claude-sonnet-4-6")
            == "anthropic:default"
        )

    def test_opencode_provider_prefix(self):
        from kew.audit import configured_endpoint

        assert (
            configured_endpoint("opencode", "ollama/llama3.3") == "ollama:via-opencode"
        )
        assert (
            configured_endpoint("opencode", "openrouter/qwen-2.5-coder")
            == "openrouter:via-opencode"
        )

    def test_opencode_no_model(self):
        from kew.audit import configured_endpoint

        assert configured_endpoint("opencode", "") == "unknown:via-opencode"

    @pytest.mark.parametrize("model", ["gpt-5.4", ""])
    def test_codex_uses_fixed_official_endpoint_identity(self, model):
        from kew.audit import configured_endpoint

        assert configured_endpoint("codex", model) == "openai:via-codex"

    def test_pi_provider_prefix(self):
        from kew.audit import configured_endpoint

        assert (
            configured_endpoint("pi", "anthropic/claude-sonnet-4-6")
            == "anthropic:via-pi"
        )
        assert configured_endpoint("pi", "") == "unknown:via-pi"

    def test_unknown_runner_returns_empty(self):
        from kew.audit import configured_endpoint

        assert configured_endpoint("mystery-runner", "some/model") == ""

    def test_base_url_credentials_are_stripped(self, monkeypatch):
        from kew.audit import configured_endpoint

        monkeypatch.setenv(
            "ANTHROPIC_BASE_URL", "https://user:secret@proxy.corp:8443/v1?key=tok#f"
        )
        assert (
            configured_endpoint("claude-code", "claude-sonnet-4-6")
            == "https://proxy.corp:8443/v1"
        )

    def test_non_url_endpoint_values_pass_through(self):
        from kew.audit import _sanitize_endpoint_url

        assert _sanitize_endpoint_url("anthropic:default") == "anthropic:default"
        assert _sanitize_endpoint_url("") == ""


def _emit_worker(
    db_path: str, worker_id: int, n_events: int, barrier: multiprocessing.Barrier
) -> None:
    """One real OS process emitting n_events through its own connection."""
    from kew.audit import Actor, emit
    from kew.run_store import SqliteRunStore

    barrier.wait(timeout=30)  # all workers start their first INSERT together
    with SqliteRunStore(db_path) as store:
        for i in range(n_events):
            emit(
                store,
                "dispatch_completed",
                actor=Actor("human", f"worker-{worker_id}"),
                issue_num=worker_id,
                payload={"worker": worker_id, "seq": i},
            )


class TestConcurrentEmit:
    """N real processes emitting simultaneously: N rows, strict id order."""

    def test_concurrent_emits_land_with_strictly_ordered_ids(self, tmp_path):
        n_procs, n_events = 8, 25
        db_path = str(tmp_path / "audit.db")
        # Pre-create (and migrate) the DB so workers race on writes,
        # not on the migration itself; double-migration is separately
        # covered in TestSchemaV8AuditEnvelope.
        with SqliteRunStore(db_path) as setup:
            assert setup.list_audit_events() == []

        ctx = multiprocessing.get_context("spawn")
        barrier = ctx.Barrier(n_procs)
        procs = [
            ctx.Process(target=_emit_worker, args=(db_path, i, n_events, barrier))
            for i in range(n_procs)
        ]
        try:
            for p in procs:
                p.start()
            for p in procs:
                p.join(timeout=60)
            assert all(p.exitcode == 0 for p in procs), [p.exitcode for p in procs]
        finally:
            for p in procs:
                if p.is_alive():
                    p.terminate()
                    p.join(5)

        with SqliteRunStore(db_path) as store:
            events = store.list_audit_events(limit=None)
        # Every emit landed — none dropped, none raised
        assert len(events) == n_procs * n_events
        # ids are unique and strictly ordered (newest-first list)
        ids = [e.id for e in events]
        assert len(set(ids)) == len(ids)
        assert all(ids[i] > ids[i + 1] for i in range(len(ids) - 1))
        # Every worker's full sequence is present
        seen = {(e.payload["worker"], e.payload["seq"]) for e in events}
        assert seen == {(w, s) for w in range(n_procs) for s in range(n_events)}


def _seed_store(tmp_path, n=3):
    """A dispatch.db under tmp_path/logs with n audit events."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    with SqliteRunStore(str(log_dir / "dispatch.db")) as store:
        for i in range(n):
            store.append_audit_event(
                timestamp=f"2026-06-10T12:00:0{i}+00:00",
                event="dispatch_completed" if i % 2 == 0 else "fleet_stop",
                run_id=f"run-{i}",
                issue_num=i,
                repo="octo/widgets",
                actor_type="human",
                actor_id="alice",
                endpoint="anthropic:default",
                payload={"n": i},
            )
    _register_db(tmp_path, log_dir / "dispatch.db")


def _register_db(tmp_path, db_path, *, project_root=None):
    """Migrate a seeded legacy DB through the production managed boundary."""
    project_root = (
        project_root if project_root is not None else tmp_path / "project-root"
    )
    with open_managed_store(
        project_root=project_root,
        db_path=db_path,
        anchors_path=project_root / "audit-anchors.jsonl",
    ) as store:
        ledger_id = store.read_ledger_id()
    record = RegistryRecord.from_json(
        registry_paths(project_root).registry.read_text(encoding="utf-8")
    )
    assert ledger_id is not None
    assert record.ledger_id == ledger_id
    assert record.project_root == project_root.resolve()
    assert record.db_path == db_path.resolve()


def _audit_config(tmp_path):
    from kew.config import DispatchConfig

    config = DispatchConfig()
    config.logging.dir = str(tmp_path / "logs")
    return config


class TestAuditTailCli:
    """kew audit tail [--limit] [--event] [--json] (--follow loop untested
    interactively; its drain helper is covered below)."""

    def _invoke(self, tmp_path, *args):
        from unittest.mock import patch

        from kew.cli import app

        with patch("kew.audit.load_config", return_value=_audit_config(tmp_path)):
            return cli_runner.invoke(app, ["audit", "tail", *args])

    def test_tail_prints_chronological_lines(self, tmp_path):
        _seed_store(tmp_path)
        result = self._invoke(tmp_path)
        assert result.exit_code == 0
        lines = [ln for ln in result.output.splitlines() if ln.strip()]
        assert len(lines) == 4
        # oldest first for display
        assert "2026-06-10T12:00:00+00:00" in lines[0]
        assert "dispatch_completed" in lines[0]
        assert "alice" in lines[0]

    def test_tail_limit(self, tmp_path):
        _seed_store(tmp_path, n=5)
        result = self._invoke(tmp_path, "--limit", "2")
        lines = [ln for ln in result.output.splitlines() if ln.strip()]
        assert len(lines) == 2

    def test_tail_event_filter(self, tmp_path):
        _seed_store(tmp_path)
        result = self._invoke(tmp_path, "--event", "fleet_stop")
        lines = [ln for ln in result.output.splitlines() if ln.strip()]
        assert len(lines) == 1
        assert "fleet_stop" in lines[0]

    def test_tail_item_filter(self, tmp_path):
        log_dir = tmp_path / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        with SqliteRunStore(str(log_dir / "dispatch.db")) as store:
            store.append_audit_event(
                timestamp="2026-06-10T12:00:00+00:00",
                event="epic_merge",
                issue_num=0,
                item_id="SFD-12",
            )
            store.append_audit_event(
                timestamp="2026-06-10T12:00:01+00:00",
                event="epic_merge",
                issue_num=0,
                item_id="SFD-99",
            )
        _register_db(tmp_path, log_dir / "dispatch.db")
        result = self._invoke(tmp_path, "--item", "SFD-12")
        lines = [ln for ln in result.output.splitlines() if ln.strip()]
        assert len(lines) == 1
        # ids.item_ref renders a non-GitHub tracker id bare (no leading #).
        assert "SFD-12" in lines[0]
        assert "SFD-99" not in result.output

    def test_tail_renders_github_item_ref_and_issue_fallback(self, tmp_path):
        log_dir = tmp_path / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        with SqliteRunStore(str(log_dir / "dispatch.db")) as store:
            # GitHub row: item_id null, addressing falls back to #issue_num.
            store.append_audit_event(
                timestamp="2026-06-10T12:00:00+00:00",
                event="dispatch_completed",
                issue_num=42,
            )
            # GitHub row carrying a numeric item_id renders as #N (item_ref).
            store.append_audit_event(
                timestamp="2026-06-10T12:00:01+00:00",
                event="dispatch_completed",
                issue_num=7,
                item_id="7",
            )
        _register_db(tmp_path, log_dir / "dispatch.db")
        result = self._invoke(tmp_path)
        lines = [ln for ln in result.output.splitlines() if ln.strip()]
        assert "#42" in lines[0]
        assert "#7" in lines[1]

    def test_tail_json(self, tmp_path):
        import json as json_mod

        _seed_store(tmp_path)
        result = self._invoke(tmp_path, "--json")
        lines = [ln for ln in result.output.splitlines() if ln.strip()]
        parsed = [json_mod.loads(ln) for ln in lines]  # one JSON object per line
        assert parsed[0]["event"] == "dispatch_completed"
        assert parsed[0]["actor_id"] == "alice"
        assert parsed[0]["payload"] == {"n": 0}

    def test_tail_missing_db_exits_2(self, tmp_path):
        result = self._invoke(tmp_path)
        assert result.exit_code == 2

    def test_follow_drain_helper_returns_only_new_events(self, tmp_path):
        from kew.audit import _drain_new

        _seed_store(tmp_path)
        with SqliteRunStore(str(tmp_path / "logs" / "dispatch.db")) as store:
            fresh, last_id = _drain_new(store, last_id=0, event=None)
            assert [e.id for e in fresh] == [1, 2, 3, 4]  # includes identity binding
            assert last_id == 4
            fresh, last_id = _drain_new(store, last_id=4, event=None)
            assert fresh == []
            assert last_id == 4
            store.append_audit_event(
                timestamp="2026-06-10T13:00:00+00:00", event="fleet_resume"
            )
            fresh, last_id = _drain_new(store, last_id=4, event=None)
            assert len(fresh) == 1
            assert last_id == 5

    def test_follow_drain_recovers_bursts_larger_than_window(
        self, tmp_path, monkeypatch
    ):
        """A burst exceeding the poll window must not be silently skipped."""
        import kew.audit as audit
        from kew.audit import _drain_new

        monkeypatch.setattr(audit, "_FOLLOW_WINDOW", 4)
        _seed_store(tmp_path, n=10)
        with SqliteRunStore(str(tmp_path / "logs" / "dispatch.db")) as store:
            fresh, last_id = _drain_new(store, last_id=0, event=None)
            assert [e.id for e in fresh] == list(range(1, 12))  # none dropped
            assert last_id == 11

    def test_minimal_event_renders_without_error(self, tmp_path):
        """A minimal event (timestamp + event only) renders through tail without error.

        Covers _format_event_line fallback branches for missing fields.
        """
        log_dir = tmp_path / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        with SqliteRunStore(str(log_dir / "dispatch.db")) as store:
            store.append_audit_event(
                timestamp="2026-06-10T14:00:00+00:00", event="fleet_resume"
            )
        _register_db(tmp_path, log_dir / "dispatch.db")
        result = self._invoke(tmp_path)
        assert result.exit_code == 0
        assert "fleet_resume" in result.output
        # fallback placeholders for missing fields
        assert "-" in result.output


class TestAuditExport:
    """kew audit export round-trips JSON and CSV."""

    def _invoke(self, tmp_path, *args):
        from unittest.mock import patch

        from kew.cli import app

        with patch("kew.audit.load_config", return_value=_audit_config(tmp_path)):
            return cli_runner.invoke(app, ["audit", "export", *args])

    def test_export_json_round_trips(self, tmp_path):
        import json as json_mod

        _seed_store(tmp_path)
        result = self._invoke(tmp_path)
        assert result.exit_code == 0
        data = json_mod.loads(result.output)
        assert len(data) == 4
        assert data[0]["id"] == 1  # chronological
        assert data[0]["event"] == "dispatch_completed"
        assert data[0]["actor_id"] == "alice"
        assert data[0]["payload"] == {"n": 0}
        assert data[0]["policy_hash"] is None
        assert data[0]["prev_hash"] == "0" * 64  # GENESIS_PREV for first row (#249)

    def test_export_csv_has_envelope_columns(self, tmp_path):
        _seed_store(tmp_path)
        result = self._invoke(tmp_path, "-f", "csv")
        assert result.exit_code == 0
        header = result.output.splitlines()[0]
        for col in (
            "id",
            "timestamp",
            "event",
            "run_id",
            "issue_num",
            "item_id",
            "repo",
            "actor_type",
            "actor_id",
            "endpoint",
            "policy_hash",
            "prev_hash",
            "payload",
            "dedup_key",
        ):
            assert col in header
        assert "alice" in result.output

    def test_export_carries_item_id(self, tmp_path):
        import json as json_mod

        log_dir = tmp_path / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        with SqliteRunStore(str(log_dir / "dispatch.db")) as store:
            store.append_audit_event(
                timestamp="2026-06-10T12:00:00+00:00",
                event="epic_merge",
                issue_num=0,
                item_id="SFD-12",
            )
        _register_db(tmp_path, log_dir / "dispatch.db")
        data = json_mod.loads(self._invoke(tmp_path).output)
        assert data[0]["item_id"] == "SFD-12"

    def test_export_to_file(self, tmp_path):
        import json as json_mod

        _seed_store(tmp_path)
        out = tmp_path / "audit.json"
        result = self._invoke(tmp_path, "-o", str(out))
        assert result.exit_code == 0
        assert len(json_mod.loads(out.read_text())) == 4

    def test_export_since_filters(self, tmp_path):
        import json as json_mod

        _seed_store(tmp_path)
        result = self._invoke(tmp_path, "--since", "2026-06-10T12:00:01")
        data = json_mod.loads(result.output)
        assert len(data) == 3  # events at :01 and :02, plus identity binding

    def test_export_invalid_format_exits_2(self, tmp_path):
        _seed_store(tmp_path)
        assert self._invoke(tmp_path, "-f", "yaml").exit_code == 2

    def test_export_invalid_since_exits_2(self, tmp_path):
        _seed_store(tmp_path)
        assert self._invoke(tmp_path, "--since", "not-a-date").exit_code == 2


class TestAuditExportHelpers:
    """Pure export helpers (no CLI)."""

    def test_csv_payload_is_json_encoded(self):
        from kew.audit import export_audit_csv
        from kew.run_store import AuditEvent

        ev = AuditEvent(
            id=1,
            timestamp="2026-06-10T12:00:00+00:00",
            event="fleet_stop",
            payload={"reason": "runaway, with comma"},
        )
        out = export_audit_csv([ev])
        assert '"{""reason"": ""runaway, with comma""}"' in out

    def test_json_empty_list(self):
        from kew.audit import export_audit_json

        assert export_audit_json([]) == "[]"


# ---------------------------------------------------------------------------
# kew audit verify (#249)
# ---------------------------------------------------------------------------
from typer.testing import CliRunner as VerifyRunner  # noqa: E402

from kew.audit import audit_app  # noqa: E402
from kew.audit_chain import Anchor, entry_hash, format_anchor  # noqa: E402

runner = VerifyRunner()


def _seed_db(path, n=3, *, project_root=None):
    s = SqliteRunStore(str(path))
    for i in range(n):
        s.append_audit_event(
            timestamp="2026-06-25T00:00:00+00:00",
            event=f"e{i}",
            actor_type="human",
            actor_id="tom",
        )
    s.close()
    root = project_root if project_root is not None else path.parent / "project-root"
    with open_managed_store(
        project_root=root,
        db_path=path,
        anchors_path=root / "audit-anchors.jsonl",
    ) as store:
        ledger_id = store.read_ledger_id()
    assert ledger_id is not None
    return ledger_id


def _seed_identity_db(path, project_root, n=2):
    with open_managed_store(
        project_root=project_root,
        db_path=path,
        anchors_path=project_root / "audit-anchors.jsonl",
    ) as store:
        for i in range(n):
            store.append_audit_event(
                timestamp="2026-06-25T00:00:00+00:00",
                event=f"e{i}",
                actor_type="human",
                actor_id="tom",
            )
        ledger_id = store._conn.execute(
            "SELECT ledger_id FROM ledger_identity WHERE id = 1"
        ).fetchone()[0]
    return ledger_id


# Convention: drive the db path via `monkeypatch.setenv("KEW_LOG_DIR", tmp_path)`
# (load_config reads os.environ uncached at call time, so _resolve_db_path
# resolves tmp_path/dispatch.db) and invoke the audit sub-app directly:
# `runner.invoke(audit_app, [...])`. Do NOT mix in the older
# patch("kew.audit.load_config")+tmp_path/logs style from earlier tests in
# this file — pick this one convention for all #249 verify/anchor tests.
#
# Tests that exercise the *default* anchors path must also set KEW_PROJECT to
# tmp_path: since #1156 that default resolves against the project root, so a
# bare `verify` from the checkout would otherwise read the repo's real
# audit-anchors.jsonl.


def test_verify_intact_exits_0(tmp_path, monkeypatch):
    monkeypatch.setenv("KEW_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("KEW_PROJECT", str(tmp_path))
    _seed_db(tmp_path / "dispatch.db")
    _register_db(tmp_path, tmp_path / "dispatch.db")
    res = runner.invoke(audit_app, ["verify"])
    assert res.exit_code == 0


def test_verify_empty_table_exits_0(tmp_path, monkeypatch):
    monkeypatch.setenv("KEW_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("KEW_PROJECT", str(tmp_path))
    SqliteRunStore(str(tmp_path / "dispatch.db")).close()
    _register_db(tmp_path, tmp_path / "dispatch.db")
    res = runner.invoke(audit_app, ["verify"])
    assert res.exit_code == 0


def test_verify_missing_db_exits_2(tmp_path, monkeypatch):
    monkeypatch.setenv("KEW_LOG_DIR", str(tmp_path))
    res = runner.invoke(audit_app, ["verify"])
    assert res.exit_code == 2


def test_verify_refuses_unregistered_ledger(tmp_path, monkeypatch):
    """A seeded DB without registration is not an audit ledger."""
    monkeypatch.setenv("KEW_LOG_DIR", str(tmp_path))
    db = tmp_path / "dispatch.db"
    with SqliteRunStore(str(db)) as store:
        store.append_audit_event(timestamp="t", event="e0")
    before = db.read_bytes()

    res = runner.invoke(audit_app, ["verify"])

    assert res.exit_code == 2
    assert "Refusing to read the audit ledger" in res.stderr
    assert "REGISTERED-LEDGER-MISSING" in res.stderr
    assert db.read_bytes() == before
    assert not registry_paths(tmp_path / "project-root").registry.exists()


def test_verify_refuses_registered_ledger_whose_database_is_gone(tmp_path, monkeypatch):
    monkeypatch.setenv("KEW_LOG_DIR", str(tmp_path))
    db = tmp_path / "dispatch.db"
    _seed_db(db, n=1)
    _register_db(tmp_path, db)
    db.unlink()

    res = runner.invoke(audit_app, ["verify"])

    assert res.exit_code == 2
    assert not db.exists()


def test_verify_refuses_project_ledger_mismatch(tmp_path, monkeypatch):
    monkeypatch.setenv("KEW_LOG_DIR", str(tmp_path))
    db = tmp_path / "dispatch.db"
    other_root = tmp_path / "other-project"
    other_root.mkdir()
    (other_root / ".git").mkdir()
    _seed_db(db, n=1, project_root=other_root)

    res = runner.invoke(audit_app, ["verify"])

    assert res.exit_code == 2
    assert "Refusing to read the audit ledger" in res.stderr
    assert "REGISTRY-MISSING-FOR-IDENTITY" in res.stderr
    assert not registry_paths(tmp_path / "project-root").registry.exists()


def test_verify_tamper_exits_1(tmp_path, monkeypatch):
    monkeypatch.setenv("KEW_LOG_DIR", str(tmp_path))
    _seed_db(tmp_path / "dispatch.db", n=3)
    _register_db(tmp_path, tmp_path / "dispatch.db")
    s = SqliteRunStore(str(tmp_path / "dispatch.db"))
    s._conn.execute("UPDATE audit_events SET actor_id='mallory' WHERE id=1")
    s._conn.commit()
    s.close()
    res = runner.invoke(audit_app, ["verify"])
    assert res.exit_code == 1


def test_verify_lagging_anchor_beyond_window_exits_1(tmp_path, monkeypatch):
    # #912: a count-shrink / stale-lagging anchor (live chain far behind the
    # latest anchor) is no longer advisory — it fails closed by default.
    monkeypatch.setenv("KEW_LOG_DIR", str(tmp_path))
    ledger_id = _seed_db(tmp_path / "dispatch.db", n=2)
    _register_db(tmp_path, tmp_path / "dispatch.db")
    anchors = tmp_path / "anchors.jsonl"
    anchors.write_text(
        format_anchor(
            Anchor(
                id=99,
                entry_hash="f" * 64,
                count=99,
                timestamp="t",
                prev="0" * 64,
                version=2,
                ledger_id=ledger_id,
            )
        )
        + "\n"
    )
    res = runner.invoke(audit_app, ["verify", "--anchors", str(anchors)])
    assert res.exit_code == 1


def test_verify_lagging_anchor_within_window_exits_0(tmp_path, monkeypatch):
    # A lag inside the configured staleness window is tolerated (exit 0).
    monkeypatch.setenv("KEW_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("KEW_AUDIT_STALENESS_WINDOW", "200")
    ledger_id = _seed_db(tmp_path / "dispatch.db", n=2)
    _register_db(tmp_path, tmp_path / "dispatch.db")
    anchors = tmp_path / "anchors.jsonl"
    anchors.write_text(
        format_anchor(
            Anchor(
                id=99,
                entry_hash="f" * 64,
                count=99,
                timestamp="t",
                prev="0" * 64,
                version=2,
                ledger_id=ledger_id,
            )
        )
        + "\n"
    )
    res = runner.invoke(audit_app, ["verify", "--anchors", str(anchors)])
    assert res.exit_code == 0


def test_verify_count_shrink_relative_to_latest_anchor_exits_1(tmp_path, monkeypatch):
    # The latest anchor's count exceeds the (matching-hash) live chain: rows were
    # truncated after anchoring. Fails closed even though the head hash matches.
    monkeypatch.setenv("KEW_LOG_DIR", str(tmp_path))
    ledger_id = _seed_db(tmp_path / "dispatch.db", n=2)
    _register_db(tmp_path, tmp_path / "dispatch.db")
    with SqliteRunStore(str(tmp_path / "dispatch.db")) as s:
        rows = s.read_chain_rows()
    head = entry_hash(rows[-1])
    anchors = tmp_path / "anchors.jsonl"
    anchors.write_text(
        format_anchor(
            Anchor(
                id=rows[-1].id,
                entry_hash=head,
                count=50,
                timestamp="t",
                version=2,
                ledger_id=ledger_id,
            )
        )
        + "\n"
    )
    res = runner.invoke(audit_app, ["verify", "--anchors", str(anchors)])
    assert res.exit_code == 1
    j = json.loads(
        runner.invoke(audit_app, ["verify", "--anchors", str(anchors), "--json"]).stdout
    )
    assert j["ok"] is False and j["anchors"]["count_shrink"] is True


def test_verify_rechained_anchor_history_exits_1(tmp_path, monkeypatch):
    # #912: anchor-chain continuity break (re-chained anchor history) fails.
    monkeypatch.setenv("KEW_LOG_DIR", str(tmp_path))
    _seed_db(tmp_path / "dispatch.db", n=2)
    _register_db(tmp_path, tmp_path / "dispatch.db")
    with SqliteRunStore(str(tmp_path / "dispatch.db")) as s:
        rows = s.read_chain_rows()
    head = entry_hash(rows[-1])
    anchors = tmp_path / "anchors.jsonl"
    a1 = Anchor(
        id=1, entry_hash=entry_hash(rows[0]), count=1, timestamp="t", prev="0" * 64
    )
    # Second anchor's prev does NOT bind a1 → re-chained history.
    a2 = Anchor(id=2, entry_hash=head, count=2, timestamp="t", prev="d" * 64)
    anchors.write_text(format_anchor(a1) + "\n" + format_anchor(a2) + "\n")
    res = runner.invoke(audit_app, ["verify", "--anchors", str(anchors)])
    assert res.exit_code == 1
    j = json.loads(
        runner.invoke(audit_app, ["verify", "--anchors", str(anchors), "--json"]).stdout
    )
    assert (
        j["anchor_chain"]["ok"] is False and j["anchor_chain"]["broken_at_index"] == 1
    )


def test_verify_stripped_prev_after_linked_anchor_exits_1(tmp_path, monkeypatch):
    # #1122: an anchor whose "prev" was deleted, sitting after a linked anchor,
    # is a continuity break — not a legacy skip.
    monkeypatch.setenv("KEW_LOG_DIR", str(tmp_path))
    ledger_id = _seed_db(tmp_path / "dispatch.db", n=2)
    _register_db(tmp_path, tmp_path / "dispatch.db")
    with SqliteRunStore(str(tmp_path / "dispatch.db")) as s:
        rows = s.read_chain_rows()
    a1 = Anchor(
        id=1,
        entry_hash=entry_hash(rows[0]),
        count=1,
        timestamp="t",
        prev="0" * 64,
        version=2,
        ledger_id=ledger_id,
    )
    a2 = Anchor(
        id=rows[-1].id,
        entry_hash=entry_hash(rows[-1]),
        count=len(rows),
        timestamp="t",
        version=2,
        ledger_id=ledger_id,
    )
    anchors = tmp_path / "anchors.jsonl"
    anchors.write_text(format_anchor(a1) + "\n" + format_anchor(a2) + "\n")
    res = runner.invoke(audit_app, ["verify", "--anchors", str(anchors)])
    assert res.exit_code == 1
    assert "RE-CHAINED" in res.stdout
    j = json.loads(
        runner.invoke(audit_app, ["verify", "--anchors", str(anchors), "--json"]).stdout
    )
    assert j["anchor_chain"]["ok"] is False
    assert j["anchor_chain"]["broken_at_index"] == 1


def test_verify_warns_when_no_anchor_links_were_verified(tmp_path, monkeypatch):
    # #1122: "intact (0 link(s) verified)" over a multi-anchor legacy file means
    # the re-chain defence never ran — say so instead of reporting a clean bill.
    monkeypatch.setenv("KEW_LOG_DIR", str(tmp_path))
    ledger_id = _seed_db(tmp_path / "dispatch.db", n=2)
    _register_db(tmp_path, tmp_path / "dispatch.db")
    with SqliteRunStore(str(tmp_path / "dispatch.db")) as s:
        rows = s.read_chain_rows()
    anchors = tmp_path / "anchors.jsonl"
    anchors.write_text(
        format_anchor(
            Anchor(
                id=1,
                entry_hash=entry_hash(rows[0]),
                count=1,
                timestamp="t",
            )
        )
        + "\n"
        + format_anchor(
            Anchor(
                id=rows[-1].id,
                entry_hash=entry_hash(rows[-1]),
                count=len(rows),
                timestamp="t",
            )
        )
        + "\n"
        + format_anchor(
            Anchor(
                id=rows[-1].id,
                entry_hash=entry_hash(rows[-1]),
                count=len(rows),
                timestamp="t2",
                version=2,
                ledger_id=ledger_id,
            )
        )
        + "\n"
    )
    res = runner.invoke(audit_app, ["verify", "--anchors", str(anchors)])
    assert res.exit_code == 0  # legacy anchors still verify
    assert "0 link(s) verified" in res.stdout
    assert "WARNING" in res.stdout
    j = json.loads(
        runner.invoke(audit_app, ["verify", "--anchors", str(anchors), "--json"]).stdout
    )
    assert j["anchor_chain"] == {
        "ok": True,
        "broken_at_index": None,
        "checked": 0,
        "total": 3,
    }


def test_verify_single_legacy_anchor_does_not_warn(tmp_path, monkeypatch):
    # One unlinked anchor has no link to verify by definition — not a warning.
    from kew.audit_chain import anchor_hash

    monkeypatch.setenv("KEW_LOG_DIR", str(tmp_path))
    ledger_id = _seed_db(tmp_path / "dispatch.db", n=2)
    _register_db(tmp_path, tmp_path / "dispatch.db")
    with SqliteRunStore(str(tmp_path / "dispatch.db")) as s:
        rows = s.read_chain_rows()
    anchors = tmp_path / "anchors.jsonl"
    legacy = Anchor(
        id=rows[-1].id,
        entry_hash=entry_hash(rows[-1]),
        count=len(rows),
        timestamp="t",
    )
    identity_anchor = Anchor(
        id=rows[-1].id,
        entry_hash=entry_hash(rows[-1]),
        count=len(rows),
        timestamp="t2",
        prev=anchor_hash(legacy),
        version=2,
        ledger_id=ledger_id,
    )
    anchors.write_text(
        format_anchor(legacy) + "\n" + format_anchor(identity_anchor) + "\n"
    )
    res = runner.invoke(audit_app, ["verify", "--anchors", str(anchors)])
    assert res.exit_code == 0
    assert "WARNING" not in res.stdout


def test_verify_anchor_mismatch_exits_1(tmp_path, monkeypatch):
    monkeypatch.setenv("KEW_LOG_DIR", str(tmp_path))
    _seed_db(tmp_path / "dispatch.db", n=2)
    _register_db(tmp_path, tmp_path / "dispatch.db")
    anchors = tmp_path / "anchors.jsonl"
    anchors.write_text(
        format_anchor(Anchor(id=1, entry_hash="f" * 64, count=1, timestamp="t")) + "\n"
    )
    res = runner.invoke(audit_app, ["verify", "--anchors", str(anchors)])
    assert res.exit_code == 1


def test_verify_wrong_ledger_precedes_count_and_hash(tmp_path, monkeypatch):
    monkeypatch.setenv("KEW_LOG_DIR", str(tmp_path))
    ledger_id = _seed_identity_db(tmp_path / "dispatch.db", tmp_path / "project-root")
    _register_db(tmp_path, tmp_path / "dispatch.db")
    anchors = tmp_path / "anchors.jsonl"
    anchors.write_text(
        format_anchor(
            Anchor(
                id=999,
                entry_hash="f" * 64,
                count=999,
                timestamp="t",
                prev="0" * 64,
                version=2,
                ledger_id="12345678-1234-5678-9234-567812345678",
            )
        )
        + "\n"
    )

    human = runner.invoke(audit_app, ["verify", "--anchors", str(anchors)])
    machine = runner.invoke(audit_app, ["verify", "--anchors", str(anchors), "--json"])

    assert ledger_id != "12345678-1234-5678-9234-567812345678"
    assert human.exit_code == 1
    assert "WRONG-LEDGER" in human.stdout
    verdict = json.loads(machine.stdout)
    assert verdict["anchors"]["wrong_ledger"] is True
    assert verdict["anchors"]["count_shrink"] is False
    assert verdict["anchors"]["mismatched"] == []


def test_verify_identity_unanchored_is_distinct(tmp_path, monkeypatch):
    monkeypatch.setenv("KEW_LOG_DIR", str(tmp_path))
    _seed_identity_db(tmp_path / "dispatch.db", tmp_path / "project-root")
    _register_db(tmp_path, tmp_path / "dispatch.db")
    with SqliteRunStore(str(tmp_path / "dispatch.db")) as store:
        rows = store.read_chain_rows()
    anchors = tmp_path / "anchors.jsonl"
    anchors.write_text(
        format_anchor(
            Anchor(
                id=rows[-1].id,
                entry_hash=entry_hash(rows[-1]),
                count=len(rows),
                timestamp="t",
                prev="0" * 64,
            )
        )
        + "\n"
    )

    human = runner.invoke(audit_app, ["verify", "--anchors", str(anchors)])
    machine = runner.invoke(audit_app, ["verify", "--anchors", str(anchors), "--json"])

    assert human.exit_code == 1
    assert "IDENTITY-UNANCHORED" in human.stdout
    assert json.loads(machine.stdout)["anchors"]["identity_unanchored"] is True


def test_verify_unreadable_explicit_anchors_exits_2(tmp_path, monkeypatch):
    monkeypatch.setenv("KEW_LOG_DIR", str(tmp_path))
    _seed_db(tmp_path / "dispatch.db", n=1)
    _register_db(tmp_path, tmp_path / "dispatch.db")
    res = runner.invoke(
        audit_app, ["verify", "--anchors", str(tmp_path / "nope.jsonl")]
    )
    assert res.exit_code == 2


def test_verify_anchor_non_int_id_exits_2(tmp_path, monkeypatch):
    # Malformed anchor (non-int id) is a cannot-run condition, not a crash in
    # classify_anchors' int comparisons.
    monkeypatch.setenv("KEW_LOG_DIR", str(tmp_path))
    _seed_db(tmp_path / "dispatch.db", n=1)
    _register_db(tmp_path, tmp_path / "dispatch.db")
    anchors = tmp_path / "anchors.jsonl"
    anchors.write_text('{"id":"x","entry_hash":"f","count":1,"timestamp":"t"}\n')
    res = runner.invoke(audit_app, ["verify", "--anchors", str(anchors)])
    assert res.exit_code == 2


def test_verify_anchors_json_list_line_exits_2(tmp_path, monkeypatch):
    # A JSON array line makes parse_anchors' obj["id"] raise TypeError.
    monkeypatch.setenv("KEW_LOG_DIR", str(tmp_path))
    _seed_db(tmp_path / "dispatch.db", n=1)
    _register_db(tmp_path, tmp_path / "dispatch.db")
    anchors = tmp_path / "anchors.jsonl"
    anchors.write_text("[1, 2, 3]\n")
    res = runner.invoke(audit_app, ["verify", "--anchors", str(anchors)])
    assert res.exit_code == 2


# ---------------------------------------------------------------------------
# kew audit anchor (#249)
# ---------------------------------------------------------------------------


def test_anchor_appends_and_prints_head(tmp_path, monkeypatch):
    monkeypatch.setenv("KEW_LOG_DIR", str(tmp_path))
    _seed_db(tmp_path / "dispatch.db", n=3)
    anchors = tmp_path / "audit-anchors.jsonl"
    res = runner.invoke(audit_app, ["anchor", "--anchors", str(anchors)])
    assert res.exit_code == 0
    lines = anchors.read_text().splitlines()
    assert len(lines) == 1
    import json as _json

    rec = _json.loads(lines[0])
    assert rec["id"] == 4 and rec["count"] == 4 and len(rec["entry_hash"]) == 64
    assert rec["entry_hash"] in res.stdout
    assert f"anchors: {anchors.resolve()}" in res.stderr
    assert "warning: creating a new anchors file" in res.stderr


def test_anchor_appends_second_line(tmp_path, monkeypatch):
    monkeypatch.setenv("KEW_LOG_DIR", str(tmp_path))
    _seed_db(tmp_path / "dispatch.db", n=1)
    anchors = tmp_path / "audit-anchors.jsonl"
    res1 = runner.invoke(audit_app, ["anchor", "--anchors", str(anchors)])
    assert "warning:" in res1.stderr  # first-time warning
    with SqliteRunStore(str(tmp_path / "dispatch.db")) as store:
        store.append_audit_event(timestamp="t", event="after_first_anchor")
    res2 = runner.invoke(audit_app, ["anchor", "--anchors", str(anchors)])
    assert "warning:" not in res2.stderr  # no warning on subsequent
    assert len(anchors.read_text().splitlines()) == 2


def test_anchor_first_binds_genesis_prev(tmp_path, monkeypatch):
    # #912: the first-ever anchor's prev is GENESIS_PREV (bootstraps the chain).
    monkeypatch.setenv("KEW_LOG_DIR", str(tmp_path))
    _seed_db(tmp_path / "dispatch.db", n=2)
    anchors = tmp_path / "audit-anchors.jsonl"
    runner.invoke(audit_app, ["anchor", "--anchors", str(anchors)])
    rec = json.loads(anchors.read_text().splitlines()[0])
    assert rec["prev"] == "0" * 64


def test_anchor_second_binds_previous_and_chain_verifies(tmp_path, monkeypatch):
    # #912: the second anchor's prev binds the first anchor; the resulting file
    # passes verify_anchor_chain end to end.
    from kew.audit_chain import parse_anchors, verify_anchor_chain

    monkeypatch.setenv("KEW_LOG_DIR", str(tmp_path))
    _seed_db(tmp_path / "dispatch.db", n=1)
    anchors = tmp_path / "audit-anchors.jsonl"
    runner.invoke(audit_app, ["anchor", "--anchors", str(anchors)])
    _seed_db(tmp_path / "dispatch.db", n=2)  # extend the chain, then re-anchor
    runner.invoke(audit_app, ["anchor", "--anchors", str(anchors)])
    parsed = parse_anchors(anchors.read_text())
    assert len(parsed) == 2
    assert verify_anchor_chain(parsed).ok


def test_anchor_transitions_legacy_history_to_v2_and_binds_legacy_head(
    tmp_path, monkeypatch
):
    from kew.audit_chain import anchor_hash, parse_anchors

    monkeypatch.setenv("KEW_LOG_DIR", str(tmp_path))
    ledger_id = _seed_identity_db(tmp_path / "dispatch.db", tmp_path / "project-root")
    with SqliteRunStore(str(tmp_path / "dispatch.db")) as store:
        rows = store.read_chain_rows()
    legacy = Anchor(
        id=rows[0].id,
        entry_hash=entry_hash(rows[0]),
        count=1,
        timestamp="legacy",
        prev="0" * 64,
    )
    anchors = tmp_path / "audit-anchors.jsonl"
    anchors.write_text(format_anchor(legacy) + "\n")

    result = runner.invoke(audit_app, ["anchor", "--anchors", str(anchors)])

    assert result.exit_code == 0
    parsed = parse_anchors(anchors.read_text())
    assert parsed[0] == legacy
    assert parsed[1].version == 2
    assert parsed[1].ledger_id == ledger_id
    assert parsed[1].prev == anchor_hash(legacy)


def test_anchor_v2_transition_retry_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("KEW_LOG_DIR", str(tmp_path))
    _seed_identity_db(tmp_path / "dispatch.db", tmp_path / "project-root")
    anchors = tmp_path / "audit-anchors.jsonl"

    first = runner.invoke(audit_app, ["anchor", "--anchors", str(anchors)])
    before = anchors.read_bytes()
    retry = runner.invoke(audit_app, ["anchor", "--anchors", str(anchors)])

    assert first.exit_code == retry.exit_code == 0
    assert anchors.read_bytes() == before


def test_anchor_refuses_existing_v2_history_for_another_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("KEW_LOG_DIR", str(tmp_path))
    _seed_identity_db(tmp_path / "dispatch.db", tmp_path / "project-root")
    anchors = tmp_path / "audit-anchors.jsonl"
    foreign = Anchor(
        id=1,
        entry_hash="a" * 64,
        count=1,
        timestamp="t",
        prev="0" * 64,
        version=2,
        ledger_id="12345678-1234-5678-9234-567812345678",
    )
    anchors.write_text(format_anchor(foreign) + "\n")
    before = anchors.read_bytes()

    result = runner.invoke(audit_app, ["anchor", "--anchors", str(anchors)])

    assert result.exit_code == 1
    assert "WRONG-LEDGER" in result.stderr
    assert anchors.read_bytes() == before


def test_anchor_default_path_lands_at_the_project_root_not_the_cwd(
    tmp_path, monkeypatch
):
    """#1156/#1120: `kew audit anchor` from a subdir wrote a stray anchors file."""
    project = (tmp_path / "repo").resolve()
    (project / ".git").mkdir(parents=True)
    (project / "kew.toml").write_text('[project]\nname = "repo"\n', encoding="utf-8")
    (project / "kew").mkdir()
    monkeypatch.setenv("KEW_LOG_DIR", str(tmp_path))
    monkeypatch.setattr("kew.audit.project_root", lambda: project)
    monkeypatch.chdir(project / "kew")
    _seed_db(tmp_path / "dispatch.db", n=2, project_root=project)

    res = runner.invoke(audit_app, ["anchor"])

    assert res.exit_code == 0
    anchor_file = project / "audit-anchors.jsonl"
    assert anchor_file.is_file()
    assert not (project / "kew" / "audit-anchors.jsonl").exists()
    assert len(anchor_file.read_text().splitlines()) == 1
    assert f"anchors: {anchor_file}" in res.stderr


def test_verify_default_anchors_read_from_the_project_root(tmp_path, monkeypatch):
    """The verdict must not depend on which directory the operator stands in."""
    project = (tmp_path / "repo").resolve()
    (project / ".git").mkdir(parents=True)
    (project / "kew.toml").write_text('[project]\nname = "repo"\n', encoding="utf-8")
    (project / "kew").mkdir()
    monkeypatch.setenv("KEW_LOG_DIR", str(tmp_path))
    monkeypatch.setattr("kew.audit.project_root", lambda: project)
    _seed_db(tmp_path / "dispatch.db", n=2, project_root=project)
    _register_db(tmp_path, tmp_path / "dispatch.db", project_root=project)
    monkeypatch.chdir(project)
    runner.invoke(audit_app, ["anchor"])

    monkeypatch.chdir(project / "kew")
    res = runner.invoke(audit_app, ["verify"])

    assert res.exit_code == 0
    assert "1 matched" in res.stdout


def test_explicit_relative_anchors_path_stays_relative_to_the_cwd(
    tmp_path, monkeypatch
):
    """An operator-typed relative path means what they typed, project or not."""
    project = (tmp_path / "repo").resolve()
    (project / ".git").mkdir(parents=True)
    (project / "kew.toml").write_text('[project]\nname = "repo"\n', encoding="utf-8")
    (project / "kew").mkdir()
    monkeypatch.setenv("KEW_LOG_DIR", str(tmp_path))
    monkeypatch.setattr("kew.audit.project_root", lambda: project)
    monkeypatch.chdir(project / "kew")
    _seed_db(tmp_path / "dispatch.db", n=1, project_root=project)

    res = runner.invoke(audit_app, ["anchor", "--anchors", "here.jsonl"])

    assert res.exit_code == 0
    assert (project / "kew" / "here.jsonl").is_file()


def test_anchor_refuses_rechained_anchor_history(tmp_path, monkeypatch):
    # A tampered anchor FILE (broken continuity) must not be extended.
    monkeypatch.setenv("KEW_LOG_DIR", str(tmp_path))
    _seed_db(tmp_path / "dispatch.db", n=2)
    anchors = tmp_path / "audit-anchors.jsonl"
    a1 = Anchor(id=1, entry_hash="a" * 64, count=1, timestamp="t", prev="0" * 64)
    a2 = Anchor(id=2, entry_hash="b" * 64, count=2, timestamp="t", prev="d" * 64)
    anchors.write_text(format_anchor(a1) + "\n" + format_anchor(a2) + "\n")
    res = runner.invoke(audit_app, ["anchor", "--anchors", str(anchors)])
    assert res.exit_code == 1
    assert len(anchors.read_text().splitlines()) == 2  # nothing appended


def test_anchor_empty_operational_chain_records_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("KEW_LOG_DIR", str(tmp_path))
    SqliteRunStore(str(tmp_path / "dispatch.db")).close()
    _register_db(tmp_path, tmp_path / "dispatch.db")
    anchors = tmp_path / "audit-anchors.jsonl"
    res = runner.invoke(audit_app, ["anchor", "--anchors", str(anchors)])
    assert res.exit_code == 0
    record = json.loads(anchors.read_text())
    assert record["id"] == record["count"] == 1


def test_anchor_missing_db_exits_2(tmp_path, monkeypatch):
    monkeypatch.setenv("KEW_LOG_DIR", str(tmp_path))
    res = runner.invoke(audit_app, ["anchor", "--anchors", str(tmp_path / "a.jsonl")])
    assert res.exit_code == 2


def test_anchor_refuses_broken_chain(tmp_path, monkeypatch):
    # A tampered local chain must NOT be anchored (that would bless the tamper).
    monkeypatch.setenv("KEW_LOG_DIR", str(tmp_path))
    _seed_db(tmp_path / "dispatch.db", n=3)
    s = SqliteRunStore(str(tmp_path / "dispatch.db"))
    s._conn.execute("UPDATE audit_events SET actor_id='mallory' WHERE id=1")
    s._conn.commit()
    s.close()
    anchors = tmp_path / "audit-anchors.jsonl"
    res = runner.invoke(audit_app, ["anchor", "--anchors", str(anchors)])
    assert res.exit_code != 0
    assert not anchors.exists()  # no anchor record written


def test_anchor_new_file_warns_for_non_empty_chain(tmp_path, monkeypatch):
    """#1120: creating a new anchors file for a chain with events emits a warning."""
    monkeypatch.setenv("KEW_LOG_DIR", str(tmp_path))
    _seed_db(tmp_path / "dispatch.db", n=2)
    anchors = tmp_path / "audit-anchors.jsonl"
    assert not anchors.exists()

    res = runner.invoke(audit_app, ["anchor", "--anchors", str(anchors)])

    assert res.exit_code == 0
    assert "warning: creating a new anchors file" in res.stderr
    assert "3 event(s)" in res.stderr


# ---------------------------------------------------------------------------
# Policy context binding (#250)
# ---------------------------------------------------------------------------


def test_set_audit_context_binds_and_clears_all_three(tmp_path):
    import kew.audit as audit

    audit.set_audit_context(repo="o/r", policy_hash="hh", snapshot_json='{"v":1}')
    assert audit._audit_repo == "o/r"
    assert audit.current_policy_hash() == "hh"
    assert audit.current_policy_snapshot() == '{"v":1}'
    # Calling with repo only clears policy hash + snapshot (the reset fixture path).
    audit.set_audit_context(repo=None)
    assert audit._audit_repo is None
    assert audit.current_policy_hash() is None
    assert audit.current_policy_snapshot() is None


def test_emit_carries_bound_policy_hash(tmp_path):
    import kew.audit as audit
    from kew.run_store import SqliteRunStore

    db = tmp_path / "dispatch.db"
    with SqliteRunStore(str(db)) as store:
        audit.set_audit_context(repo="o/r", policy_hash="pol-abc", snapshot_json="{}")
        audit.emit(store, "test_event", actor=audit.Actor("human", "u"))
        events = store.list_audit_events(10)
    assert events[0].policy_hash == "pol-abc"


def test_bind_policy_context_sets_hash_from_config():
    import kew.audit as audit
    from kew.config import DispatchConfig

    audit.bind_policy_context(DispatchConfig(project={"repo": "o/r"}))
    assert audit.current_policy_hash() is not None
    assert audit.current_policy_snapshot() is not None


def test_bind_policy_context_degrades_on_compute_failure(monkeypatch):
    import kew.audit as audit
    from kew.config import DispatchConfig

    def boom(_config, **_kw):
        raise RuntimeError("compute exploded")

    monkeypatch.setattr("kew.policy.compute_policy", boom)
    audit.bind_policy_context(DispatchConfig(project={"repo": "o/r"}))
    assert audit._audit_repo == "o/r"
    assert audit.current_policy_hash() is None  # bound repo only, did not raise


def test_record_current_policy_persists_when_bound(tmp_path):
    import kew.audit as audit
    from kew.run_store import SqliteRunStore

    with SqliteRunStore(str(tmp_path / "dispatch.db")) as store:
        audit.set_audit_context(repo="o/r", policy_hash="ph", snapshot_json='{"v":1}')
        audit.record_current_policy(store)
        row = store.get_policy_snapshot("ph")
    assert row is not None and row[0] == '{"v":1}'


def test_record_current_policy_noop_when_unbound(tmp_path):
    import kew.audit as audit
    from kew.run_store import SqliteRunStore

    with SqliteRunStore(str(tmp_path / "dispatch.db")) as store:
        audit.set_audit_context(repo="o/r")  # no policy bound
        audit.record_current_policy(store)  # must not raise
        count = store._conn.execute(
            "SELECT COUNT(*) AS n FROM policy_snapshots"
        ).fetchone()["n"]
    assert count == 0


def test_record_current_policy_swallows_store_error(monkeypatch):
    import kew.audit as audit

    class Boom:
        def record_policy_snapshot(self, *_a):
            raise RuntimeError("disk full")

    audit.set_audit_context(repo="o/r", policy_hash="ph", snapshot_json="{}")
    audit.record_current_policy(Boom())  # best-effort: logs, does not raise


# ---------------------------------------------------------------------------
# kew audit policy (#250)
# ---------------------------------------------------------------------------


def test_audit_policy_current_prints_hash(monkeypatch, tmp_path):
    from typer.testing import CliRunner

    from kew.audit import audit_app
    from kew.config import DispatchConfig

    monkeypatch.setattr("kew.audit.load_config", lambda: DispatchConfig())
    monkeypatch.chdir(tmp_path)  # hermetic: no .claude hook here
    result = CliRunner().invoke(audit_app, ["policy"])
    assert result.exit_code == 0
    assert "policy_hash:" in result.stdout


def test_audit_policy_resolves_known_hash(monkeypatch, tmp_path):
    from typer.testing import CliRunner

    from kew.audit import audit_app
    from kew.run_store import SqliteRunStore

    db = tmp_path / "dispatch.db"
    with SqliteRunStore(str(db)) as store:
        store.record_policy_snapshot("known", '{"v":1}', "2026-06-25T00:00:00+00:00")
    _register_db(tmp_path, db)
    monkeypatch.setenv("KEW_LOG_DIR", str(tmp_path))
    calls = _recorded_managed_open(monkeypatch)
    result = CliRunner().invoke(audit_app, ["policy", "known"])
    assert result.exit_code == 0
    assert "first_seen: 2026-06-25T00:00:00+00:00" in result.stdout
    assert calls
    assert all(call["read_only"] is True for call in calls)


def test_audit_policy_unknown_hash_exits_1(monkeypatch, tmp_path):
    from typer.testing import CliRunner

    from kew.audit import audit_app
    from kew.run_store import SqliteRunStore

    db = tmp_path / "dispatch.db"
    with SqliteRunStore(str(db)):
        pass
    _register_db(tmp_path, db)
    monkeypatch.setenv("KEW_LOG_DIR", str(tmp_path))
    result = CliRunner().invoke(audit_app, ["policy", "missing"])
    assert result.exit_code == 1
    assert "not retained" in result.stdout + result.stderr


def test_audit_policy_no_db_exits_2(monkeypatch, tmp_path):
    from typer.testing import CliRunner

    from kew.audit import audit_app

    monkeypatch.setenv("KEW_LOG_DIR", str(tmp_path))
    result = CliRunner().invoke(audit_app, ["policy", "anyhash"])
    assert result.exit_code == 2


def test_audit_policy_refuses_unregistered_ledger(monkeypatch, tmp_path):
    db = tmp_path / "dispatch.db"
    with SqliteRunStore(str(db)) as store:
        store.record_policy_snapshot("known", '{"v":1}', "2026-06-25T00:00:00+00:00")
    before = db.read_bytes()
    monkeypatch.setenv("KEW_LOG_DIR", str(tmp_path))

    result = CliRunner().invoke(audit_app, ["policy", "known"])

    assert result.exit_code == 2
    assert "Refusing to read the audit ledger" in result.stderr
    assert "REGISTERED-LEDGER-MISSING" in result.stderr
    assert db.read_bytes() == before
    assert not registry_paths(tmp_path / "project-root").registry.exists()


# ---------------------------------------------------------------------------
# Audit reads open the *registered* ledger read-only (#1408, #1661)
# ---------------------------------------------------------------------------


def _recorded_managed_open(monkeypatch):
    """Record every managed open the CLI performs, then delegate to the real one."""
    calls: list[dict] = []

    def _record(**kwargs):
        calls.append(kwargs)
        return open_managed_store(**kwargs)

    monkeypatch.setattr("kew.audit.open_managed_store", _record)
    return calls


def test_verify_uses_read_only_managed_open(tmp_path, monkeypatch):
    monkeypatch.setenv("KEW_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("KEW_PROJECT", str(tmp_path))
    db = tmp_path / "dispatch.db"
    _seed_db(db, n=2)
    _register_db(tmp_path, db)
    calls = _recorded_managed_open(monkeypatch)

    res = runner.invoke(audit_app, ["verify"])

    assert res.exit_code == 0, res.output
    assert calls, "verify did not go through the managed ledger boundary"
    assert all(call["read_only"] is True for call in calls)
    assert calls[0]["project_root"] == tmp_path / "project-root"
    assert calls[0]["db_path"] == db.resolve()


def test_anchor_uses_read_only_managed_open(tmp_path, monkeypatch):
    """Anchoring reads; it must never migrate, create, or register a ledger."""
    monkeypatch.setenv("KEW_LOG_DIR", str(tmp_path))
    _seed_db(tmp_path / "dispatch.db", n=2)
    calls = _recorded_managed_open(monkeypatch)
    anchors = tmp_path / "audit-anchors.jsonl"

    res = runner.invoke(audit_app, ["anchor", "--anchors", str(anchors)])

    assert res.exit_code == 0, res.output
    assert calls, "anchor did not go through the managed ledger boundary"
    assert all(call["read_only"] is True for call in calls)
    assert calls[0]["project_root"] == tmp_path / "project-root"
    assert calls[0]["db_path"] == (tmp_path / "dispatch.db").resolve()
    assert len(anchors.read_text().splitlines()) == 1


def test_anchor_refuses_missing_registration_before_write(tmp_path, monkeypatch):
    """An unregistered DB is not a ledger — refuse, and write no anchor."""
    monkeypatch.setenv("KEW_LOG_DIR", str(tmp_path))
    db = tmp_path / "dispatch.db"
    with SqliteRunStore(str(db)) as store:
        store.append_audit_event(
            timestamp="2026-08-16T00:00:00+00:00",
            event="e0",
            actor_type="human",
            actor_id="tom",
        )
    assert not registry_paths(tmp_path / "project-root").registry.exists()
    anchors = tmp_path / "audit-anchors.jsonl"

    res = runner.invoke(audit_app, ["anchor", "--anchors", str(anchors)])

    assert res.exit_code == 2
    assert "Refusing to anchor" in res.stderr
    assert "REGISTERED-LEDGER-MISSING" in res.stderr
    assert not anchors.exists()
    # Nothing was created on the refusal path.
    assert not registry_paths(tmp_path / "project-root").registry.exists()


def test_anchor_refuses_a_registered_ledger_whose_database_is_gone(
    tmp_path, monkeypatch
):
    """A registration pointing at a deleted DB must not silently re-mint one."""
    monkeypatch.setenv("KEW_LOG_DIR", str(tmp_path))
    _seed_db(tmp_path / "dispatch.db", n=1)
    (tmp_path / "dispatch.db").unlink()
    anchors = tmp_path / "audit-anchors.jsonl"

    res = runner.invoke(audit_app, ["anchor", "--anchors", str(anchors)])

    assert res.exit_code == 2
    assert not anchors.exists()
    assert not (tmp_path / "dispatch.db").exists()


def test_anchor_refuses_a_project_ledger_mismatch(tmp_path, monkeypatch):
    """A DB registered to another project root is the WRONG-LEDGER, not a fallback."""
    monkeypatch.setenv("KEW_LOG_DIR", str(tmp_path))
    other_root = tmp_path / "other-project"
    other_root.mkdir()
    (other_root / ".git").mkdir()
    _seed_db(tmp_path / "dispatch.db", n=2, project_root=other_root)
    # The registry the CLI consults (tmp_path/project-root) has no record, and
    # the database identity is bound to a different project.
    anchors = tmp_path / "audit-anchors.jsonl"

    res = runner.invoke(audit_app, ["anchor", "--anchors", str(anchors)])

    assert res.exit_code == 2
    assert "Refusing to anchor" in res.stderr
    assert not anchors.exists()


def test_anchor_resolves_the_database_from_the_explicit_project(tmp_path, monkeypatch):
    """No KEW_LOG_DIR: the configured, project-anchored logging.dir wins."""
    project = tmp_path / "project-root"
    project.mkdir(exist_ok=True)
    (project / "kew.toml").write_text(
        '[logging]\ndir = "./logs/dispatch"\n', encoding="utf-8"
    )
    logs = project / "logs" / "dispatch"
    logs.mkdir(parents=True)
    _seed_db(logs / "dispatch.db", n=2, project_root=project)

    monkeypatch.delenv("KEW_LOG_DIR", raising=False)
    monkeypatch.setenv("KEW_PROJECT", str(project))
    calls = _recorded_managed_open(monkeypatch)
    anchors = tmp_path / "audit-anchors.jsonl"

    res = runner.invoke(audit_app, ["anchor", "--anchors", str(anchors)])

    assert res.exit_code == 0, res.output
    assert calls[0]["db_path"] == (logs / "dispatch.db").resolve()
    assert len(anchors.read_text().splitlines()) == 1


# ---------------------------------------------------------------------------
# Anchor age in the verify path (#1408 / #1121)
# ---------------------------------------------------------------------------


def _anchor_aged(tmp_path, *, days_old: float, timestamp: str | None = None):
    """Anchor the seeded chain's real head with a chosen (or unreadable) stamp."""
    from datetime import UTC, timedelta

    ledger_id = _seed_db(tmp_path / "dispatch.db", n=2)
    _register_db(tmp_path, tmp_path / "dispatch.db")
    with SqliteRunStore(str(tmp_path / "dispatch.db")) as s:
        rows = s.read_chain_rows()
    stamp = (
        timestamp
        if timestamp is not None
        else (datetime.now(UTC) - timedelta(days=days_old)).isoformat()
    )
    anchors = tmp_path / "anchors.jsonl"
    anchors.write_text(
        format_anchor(
            Anchor(
                id=rows[-1].id,
                entry_hash=entry_hash(rows[-1]),
                count=len(rows),
                timestamp=stamp,
                version=2,
                ledger_id=ledger_id,
            )
        )
        + "\n"
    )
    return anchors


def test_verify_prints_the_age_of_the_newest_anchor(tmp_path, monkeypatch):
    """An intact chain still leaves the operator blind if the witness stopped:
    `verify --anchors` reports how old the newest anchor is (#1408)."""
    monkeypatch.setenv("KEW_LOG_DIR", str(tmp_path))
    anchors = _anchor_aged(tmp_path, days_old=10)

    res = runner.invoke(audit_app, ["verify", "--anchors", str(anchors)])

    assert res.exit_code == 0, res.output
    assert "Newest anchor:" in res.stdout
    assert "10.0 day(s) old" in res.stdout


def test_verify_json_reports_the_newest_anchor_age(tmp_path, monkeypatch):
    monkeypatch.setenv("KEW_LOG_DIR", str(tmp_path))
    anchors = _anchor_aged(tmp_path, days_old=3)

    res = runner.invoke(audit_app, ["verify", "--anchors", str(anchors), "--json"])

    assert res.exit_code == 0, res.output
    payload = json.loads(res.stdout)["anchors"]
    assert payload["newest_timestamp"] is not None
    assert abs(payload["age_days"] - 3.0) < 0.01


def test_verify_reports_an_unreadable_anchor_timestamp_as_unknown(
    tmp_path, monkeypatch
):
    """A stamp that cannot be parsed is reported as unknown age, never as fresh."""
    monkeypatch.setenv("KEW_LOG_DIR", str(tmp_path))
    anchors = _anchor_aged(tmp_path, days_old=0, timestamp="t")

    res = runner.invoke(audit_app, ["verify", "--anchors", str(anchors)])

    assert res.exit_code == 0, res.output
    assert "Newest anchor: unknown" in res.stdout
    j = json.loads(
        runner.invoke(audit_app, ["verify", "--anchors", str(anchors), "--json"]).stdout
    )
    assert j["anchors"]["newest_timestamp"] is None
    assert j["anchors"]["age_days"] is None


def test_verify_without_anchors_reports_no_age(tmp_path, monkeypatch):
    """No anchors file → no age claim at all (silence, not a fresh reading)."""
    monkeypatch.setenv("KEW_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("KEW_PROJECT", str(tmp_path))
    _seed_db(tmp_path / "dispatch.db", n=2)
    _register_db(tmp_path, tmp_path / "dispatch.db")

    res = runner.invoke(audit_app, ["verify"])

    assert res.exit_code == 0, res.output
    assert "Newest anchor" not in res.stdout


def test_newest_anchor_age_takes_the_newest_readable_stamp(tmp_path):
    """Age is the newest *timestamp*, not the last line, and unreadable stamps
    never mask a readable one."""
    from datetime import UTC, timedelta

    from kew.audit import newest_anchor_age

    now = datetime.now(UTC)
    anchors = [
        Anchor(id=1, entry_hash="a" * 64, count=1, timestamp="t"),
        Anchor(id=2, entry_hash="b" * 64, count=2, timestamp=(now).isoformat()),
        Anchor(
            id=3,
            entry_hash="c" * 64,
            count=3,
            timestamp=(now - timedelta(days=5)).isoformat(),
        ),
    ]

    age = newest_anchor_age(anchors, now=now)

    assert age is not None
    assert age.newest == now
    assert age.days == 0.0
    assert newest_anchor_age([], now=now) is None
    assert (
        newest_anchor_age([Anchor(id=1, entry_hash="a", count=1, timestamp="t")])
        is None
    )


def test_newest_anchor_age_treats_a_naive_stamp_as_utc(tmp_path):
    from datetime import UTC, timedelta

    from kew.audit import newest_anchor_age

    now = datetime.now(UTC)
    naive = (now - timedelta(days=2)).replace(tzinfo=None).isoformat()

    age = newest_anchor_age(
        [Anchor(id=1, entry_hash="a" * 64, count=1, timestamp=naive)], now=now
    )

    assert age is not None
    assert abs(age.days - 2.0) < 0.01


def test_verify_flags_a_future_dated_anchor_as_clock_skew(tmp_path, monkeypatch):
    """A negative age is never printed as freshness — it names the skew."""
    monkeypatch.setenv("KEW_LOG_DIR", str(tmp_path))
    anchors = _anchor_aged(tmp_path, days_old=-4)

    res = runner.invoke(audit_app, ["verify", "--anchors", str(anchors)])

    assert res.exit_code == 0, res.output
    assert "in the future" in res.stdout
    assert "day(s) old" not in res.stdout


def test_newest_anchor_age_rejects_a_non_string_timestamp():
    """Anchor fields are copied verbatim from the file, so a numeric timestamp
    is reachable from a hand-edited line — that reads unknown, never fresh."""
    from kew.audit import newest_anchor_age
    from kew.audit_chain import parse_anchors

    line = json.dumps({"id": 1, "entry_hash": "a" * 64, "count": 1, "timestamp": 0})

    assert newest_anchor_age(parse_anchors(line + "\n")) is None


@pytest.mark.parametrize("unreadable", ["malformed", "directory"])
def test_round_four_anchor_unreadable_is_cannot_run(tmp_path, monkeypatch, unreadable):
    monkeypatch.setenv("KEW_LOG_DIR", str(tmp_path))
    _seed_db(tmp_path / "dispatch.db", n=1)
    anchors = tmp_path / "anchors.jsonl"
    if unreadable == "directory":
        anchors.mkdir()
    else:
        anchors.write_text("not json\n")
    result = runner.invoke(audit_app, ["anchor", "--anchors", str(anchors)])
    assert result.exit_code == 2, result.output
    assert "unreadable anchors file" in result.output
