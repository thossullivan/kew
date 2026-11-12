from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

import kew.readiness as readiness
from kew.inference_transport import ResolvedInferenceTransport
from kew.readiness import (
    Containment,
    ProbeCost,
    ProfileFact,
    Provenance,
    RealizedLaunchBoundary,
    RealizedProfileError,
    RealizedRouteProfile,
    ResolvedRouteProfile,
    compose_realized_profile,
    compose_route_profile,
)
from kew.run_store_readiness import (
    ProbeLeaseHeldError,
    ProbeState,
    ProbeStateError,
    ProbeVerdict,
    RouteProfileConflictError,
    RouteProfileError,
    RouteProfileMissingError,
    StoredReadinessProbe,
)

if TYPE_CHECKING:
    from pathlib import Path

    from kew.run_store import RunRecord, SqliteRunStore


def _fact(value: object, provenance: Provenance, source: str) -> ProfileFact:
    return ProfileFact(value=value, provenance=provenance, source=source)


def test_route_profile_composes_capability_and_route_ownership() -> None:
    transport = ResolvedInferenceTransport(
        mode="direct",
        requested_model="gpt-5.6-sol",
        model="gpt-5.6-sol",
        config_sha256="abc",
    )
    profile = compose_route_profile(
        runner="codex",
        requested_model="gpt-5.6-sol",
        transport=transport,
        containment=_fact("native", Provenance.ENFORCED, "sandbox decision"),
        tracker_workflow=_fact(
            ["github", "epic"], Provenance.OBSERVED, "source adapter"
        ),
        verification=_fact("pytest", Provenance.ENFORCED, "commands.verify"),
        independent_review=_fact("adversarial", Provenance.DECLARED, "review config"),
        publication=_fact("host", Provenance.ENFORCED, "publication broker"),
        metering=_fact("metered", Provenance.MEASURED, "run metering"),
        exclusions=_fact(
            ["journey certification"], Provenance.DECLARED, "route policy"
        ),
    )

    assert profile.resume.provenance is Provenance.DECLARED
    assert profile.resume.value == "session"
    assert profile.transport.value["mode"] == "direct"
    assert profile.containment.provenance is Provenance.ENFORCED
    assert profile.tracker_workflow.source == "source adapter"


def test_route_profile_round_trips_every_provenance_state() -> None:
    facts = {
        name: _fact(
            None if provenance is Provenance.UNAVAILABLE else name,
            provenance,
            "" if provenance is Provenance.UNAVAILABLE else f"{name} owner",
        )
        for name, provenance in zip(
            ResolvedRouteProfile.ROUTE_FACT_FIELDS,
            Provenance,
            strict=False,
        )
    }
    profile = compose_route_profile(
        runner="codex", requested_model="gpt-5.6-sol", **facts
    )

    restored = ResolvedRouteProfile.from_json(profile.to_json())

    assert restored == profile
    assert set(Provenance).issubset(
        {getattr(restored, name).provenance for name in restored.ALL_FACT_FIELDS}
    )

    mutable = ["later"]
    frozen = _fact(mutable, Provenance.OBSERVED, "observer")
    mutable.append("mutation")
    assert frozen.value == ("later",)


def test_material_route_and_boundary_changes_alter_fingerprint() -> None:
    original = compose_route_profile(
        runner="codex",
        requested_model="gpt-5.6-sol",
        containment=_fact("native", Provenance.ENFORCED, "sandbox"),
    )

    assert (
        replace(original, requested_model="gpt-5.7").fingerprint != original.fingerprint
    )
    changed_boundary = replace(
        original,
        containment=_fact("container:image-2", Provenance.ENFORCED, "sandbox"),
    )
    assert changed_boundary.fingerprint != original.fingerprint
    assert (
        ResolvedRouteProfile.from_dict(original.to_dict()).fingerprint
        == original.fingerprint
    )


def test_legacy_route_profile_exposes_unknowns_without_reference_lane_defaults() -> (
    None
):
    legacy = ResolvedRouteProfile.from_dict(
        {"schema_version": 0, "runner": "opencode", "requested_model": "legacy/model"}
    )

    assert legacy.runner == "opencode"
    assert all(
        getattr(legacy, field).provenance is Provenance.UNAVAILABLE
        for field in legacy.ALL_FACT_FIELDS
    )
    assert all(getattr(legacy, field).value is None for field in legacy.ALL_FACT_FIELDS)


def _boundary(**kwargs: object) -> RealizedLaunchBoundary:
    return RealizedLaunchBoundary(**kwargs)  # type: ignore[arg-type]


def _route() -> ResolvedRouteProfile:
    return compose_route_profile(runner="claude-code", requested_model="claude-opus-5")


def test_host_attempt_records_executable_identity_not_an_image() -> None:
    profile = compose_realized_profile(
        run_id="run-1",
        attempt_number=1,
        route=_route(),
        boundary=_boundary(
            containment=Containment.HOST,
            source_provider="github",
            inference_provider="anthropic",
            runner_executable="/usr/local/bin/claude",
            runner_version="2.1.0",
            runner_digest="a" * 64,
            endpoint="https://api.anthropic.com",
            service_tier="priority",
            auth_path="env:ANTHROPIC_API_KEY",
        ),
    )

    assert profile.containment.value == "host"
    assert profile.containment.provenance is Provenance.ENFORCED
    assert profile.runner_digest.provenance is Provenance.MEASURED
    assert profile.image_id.provenance is Provenance.UNAVAILABLE
    assert profile.source_provider.value == "github"
    assert profile.inference_provider.value == "anthropic"
    assert profile.auth_path.value == "env:ANTHROPIC_API_KEY"


def test_contained_attempt_records_the_resolved_image_id() -> None:
    profile = compose_realized_profile(
        run_id="run-2",
        attempt_number=1,
        route=_route(),
        boundary=_boundary(
            containment=Containment.CONTAINED,
            source_provider="linear",
            image_ref="kew-agent:latest",
            image_id="sha256:" + "b" * 64,
        ),
    )

    assert profile.containment.value == "contained"
    assert profile.image_id.value == "sha256:" + "b" * 64
    assert profile.image_ref.value == "kew-agent:latest"
    assert profile.runner_digest.provenance is Provenance.UNAVAILABLE


def test_a_mutable_tag_is_never_an_immutable_container_boundary() -> None:
    with pytest.raises(RealizedProfileError):
        _boundary(containment=Containment.CONTAINED, image_ref="kew-agent:latest")
    with pytest.raises(RealizedProfileError):
        _boundary(
            containment=Containment.CONTAINED,
            image_ref="kew-agent:latest",
            image_id="kew-agent:latest",
        )


def test_tracker_identity_cannot_be_replaced_by_an_inference_provider() -> None:
    with pytest.raises(RealizedProfileError):
        _boundary(containment=Containment.HOST, source_provider="anthropic")
    with pytest.raises(RealizedProfileError):
        _boundary(containment=Containment.HOST, source_provider="openrouter")
    with pytest.raises(RealizedProfileError):
        _boundary(containment=Containment.HOST, inference_provider="github")


def test_host_boundary_refuses_to_carry_an_image_identity() -> None:
    with pytest.raises(RealizedProfileError):
        _boundary(containment=Containment.HOST, image_ref="kew-agent:latest")
    with pytest.raises(RealizedProfileError):
        _boundary(containment=Containment.HOST, image_id="sha256:" + "c" * 64)


def test_unresolved_boundary_claims_no_identity_at_all() -> None:
    unresolved = _boundary(
        containment=Containment.UNKNOWN, image_ref="kew-agent:latest"
    )
    profile = compose_realized_profile(
        run_id="run-3", attempt_number=1, route=_route(), boundary=unresolved
    )

    assert profile.containment.provenance is Provenance.UNAVAILABLE
    assert profile.image_id.provenance is Provenance.UNAVAILABLE
    with pytest.raises(RealizedProfileError):
        _boundary(containment=Containment.UNKNOWN, image_id="sha256:" + "d" * 64)
    with pytest.raises(RealizedProfileError):
        _boundary(containment=Containment.UNKNOWN, runner_digest="e" * 64)


def test_malformed_identity_material_is_refused() -> None:
    with pytest.raises(RealizedProfileError):
        _boundary(containment=Containment.HOST, runner_digest="not-a-digest")
    with pytest.raises(RealizedProfileError):
        _boundary(containment=Containment.HOST, auth_path="Bearer sk-live-secret token")
    with pytest.raises(RealizedProfileError):
        _boundary(containment=Containment.HOST, runner_version="2.1.0\nrm -rf /")


def test_realized_profile_round_trips_and_fingerprints_material_change() -> None:
    boundary = _boundary(
        containment=Containment.CONTAINED,
        image_ref="kew-agent:latest",
        image_id="sha256:" + "f" * 64,
    )
    profile = compose_realized_profile(
        run_id="run-4", attempt_number=2, route=_route(), boundary=boundary
    )

    restored = RealizedRouteProfile.from_json(profile.to_json())
    assert restored == profile
    assert restored.fingerprint == profile.fingerprint
    assert restored.attempt_number == 2

    moved = compose_realized_profile(
        run_id="run-4",
        attempt_number=2,
        route=_route(),
        boundary=replace(boundary, image_id="sha256:" + "0" * 64),
    )
    assert moved.fingerprint != profile.fingerprint

    reconfigured = compose_realized_profile(
        run_id="run-4",
        attempt_number=2,
        route=compose_route_profile(
            runner="claude-code",
            requested_model="claude-opus-5",
            transport=ResolvedInferenceTransport(
                mode="direct",
                requested_model="claude-opus-5",
                model="claude-opus-5",
                config_sha256="changed",
            ),
        ),
        boundary=boundary,
    )
    assert reconfigured.fingerprint != profile.fingerprint


def test_sidecar_topology_round_trips_without_sensitive_values() -> None:
    evidence_type = getattr(readiness, "SidecarTopologyEvidence", None)
    packet_type = getattr(readiness, "SidecarPacketEvidence", None)
    outcome_type = getattr(readiness, "TopologyProbeResult", None)
    packet_status = getattr(readiness, "PacketProbeStatus", None)

    assert evidence_type is not None
    assert packet_type is not None
    assert outcome_type is not None
    assert packet_status is not None

    denials = tuple(
        outcome_type(
            name=name,
            positive_control=packet_status.REACHABLE,
            worker=packet_status.DENIED,
            passed=True,
        )
        for name in readiness.SIDECAR_PACKET_DENIALS
    )
    packet = packet_type(
        proxy_health=packet_status.REACHABLE,
        proxy_route=packet_status.REACHABLE,
        denials=denials,
    )
    evidence = evidence_type(
        schema_version=1,
        stage="terminal",
        run_id="run-sidecar",
        attempt_number=2,
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
        endpoint="http://192.0.2.10:4000/private?credential=never",
        observed_version="0.1.0",
        packet=packet,
        filter_verdict="passed",
        cleanup_verdict="passed",
    )

    restored = evidence_type.from_json(evidence.to_json())
    serialized = evidence.to_json()

    assert restored == evidence
    assert restored.endpoint == "http://192.0.2.10:4000"
    assert "credential=never" not in serialized
    assert "API_KEY" not in serialized


def test_legacy_realized_profile_is_all_unknowns() -> None:
    legacy = RealizedRouteProfile.from_dict(
        {"schema_version": 0, "run_id": "old", "attempt_number": 1}
    )

    assert legacy.run_id == "old"
    assert all(
        getattr(legacy, name).provenance is Provenance.UNAVAILABLE
        for name in legacy.REALIZED_FACT_FIELDS
    )
    assert all(
        getattr(legacy.route, name).provenance is Provenance.UNAVAILABLE
        for name in legacy.route.ALL_FACT_FIELDS
    )
    assert RealizedRouteProfile.unknown("old", 3).attempt_number == 3


def _store() -> SqliteRunStore:
    from kew.run_store import SqliteRunStore

    return SqliteRunStore(":memory:")


def _record(store: SqliteRunStore, run_id: str, *, attempt: int = 1) -> RunRecord:
    from kew.run_store import RunRecord

    return RunRecord(
        run_id=run_id,
        issue_num=42,
        item_id="42",
        branch="agent/issue-42",
        status="pending",
        exit_code=-1,
        started_at=datetime.now(UTC),
        runner_name="claude-code",
        model="claude-opus-5",
        attempt_number=attempt,
    )


def _profile(
    run_id: str, attempt: int = 1, **overrides: object
) -> RealizedRouteProfile:
    fields: dict[str, object] = {
        "containment": Containment.HOST,
        "source_provider": "github",
        "inference_provider": "anthropic",
        "runner_executable": "/usr/local/bin/claude",
        "runner_version": "2.1.0",
        "runner_digest": "a" * 64,
        "endpoint": "https://api.anthropic.com",
        "auth_path": "env:ANTHROPIC_API_KEY",
    }
    fields.update(overrides)
    boundary = RealizedLaunchBoundary(**fields)  # type: ignore[arg-type]
    return compose_realized_profile(
        run_id=run_id, attempt_number=attempt, route=_route(), boundary=boundary
    )


def _admit(store: SqliteRunStore, run_id: str, profile: RealizedRouteProfile) -> None:
    from kew.run_store import AdmissionIdentity

    store.admit_run_with_identity(
        _record(store, run_id),
        AdmissionIdentity(run_id=run_id, source_tracker="github", item_id="42"),
        enrich=store.route_profile_enricher(profile),
    )


def test_admission_persists_the_route_profile_in_the_same_transaction() -> None:
    store = _store()
    profile = _profile("run-a")
    _admit(store, "run-a", profile)

    stored = store.get_route_profile("run-a", 1)
    assert stored is not None
    assert stored.fingerprint == profile.fingerprint
    assert stored.image_id is None
    assert stored.runner_digest == "a" * 64
    restored = RealizedRouteProfile.restore(stored.profile_json, "run-a", 1)
    assert restored == profile
    assert restored.image_id.provenance is Provenance.UNAVAILABLE
    assert store.get("run-a") is not None


def test_a_failing_profile_write_rolls_back_the_whole_admission() -> None:
    store = _store()
    profile = _profile("run-b")

    def _boom(_cursor: object) -> None:
        raise RuntimeError("enricher exploded")

    from kew.run_store import AdmissionIdentity

    with pytest.raises(RuntimeError):
        store.admit_run_with_identity(
            _record(store, "run-b"),
            AdmissionIdentity(run_id="run-b", source_tracker="github", item_id="42"),
            enrich=_boom,
        )

    assert store.get("run-b") is None
    assert store.get_route_profile("run-b", 1) is None
    assert profile.run_id == "run-b"


def test_attempt_two_cannot_borrow_attempt_one_evidence() -> None:
    store = _store()
    _admit(store, "run-c", _profile("run-c"))

    with pytest.raises(RouteProfileMissingError):
        store.require_route_profile("run-c", 2)

    retry = _profile("run-c", 2, runner_digest="b" * 64)
    store.record_route_profile(retry)

    assert store.require_route_profile("run-c", 2).fingerprint == retry.fingerprint
    assert store.require_route_profile("run-c", 1).fingerprint != retry.fingerprint
    assert [p.attempt_number for p in store.list_route_profiles("run-c")] == [1, 2]


def test_a_recorded_profile_is_immutable_for_its_attempt() -> None:
    store = _store()
    _admit(store, "run-d", _profile("run-d"))

    store.record_route_profile(_profile("run-d"))  # byte-identical is a no-op

    with pytest.raises(RouteProfileConflictError):
        store.record_route_profile(_profile("run-d", 1, runner_version="9.9.9"))
    assert store.require_route_profile("run-d", 1).runner_version == "2.1.0"


def test_later_observations_append_without_rewriting_declared_facts() -> None:
    store = _store()
    _admit(store, "run-e", _profile("run-e"))

    store.append_route_observation(
        "run-e",
        1,
        field="reported_model",
        value="claude-opus-5",
        source="runner stream",
    )
    store.append_route_observation(
        "run-e",
        1,
        field="reported_model",
        value="claude-opus-5-20260501",
        source="runner stream",
    )

    observations = store.list_route_observations("run-e", 1)
    assert [o.value for o in observations] == [
        "claude-opus-5",
        "claude-opus-5-20260501",
    ]
    assert store.require_route_profile("run-e", 1).requested_model == ("claude-opus-5")

    with pytest.raises(RouteProfileError):
        store.append_route_observation(
            "run-e", 1, field="image_id", value="sha256:" + "c" * 64, source="forgery"
        )
    with pytest.raises(RouteProfileMissingError):
        store.append_route_observation(
            "run-e", 7, field="reported_model", value="x", source="runner stream"
        )


def test_legacy_runs_expose_unknowns_rather_than_guesses() -> None:
    store = _store()
    store.save(_record(store, "run-legacy"))

    assert store.get_route_profile("run-legacy", 1) is None
    assert store.route_profile_json("run-legacy", 1) is None
    unknown = RealizedRouteProfile.restore(None, "run-legacy", 1)
    assert unknown.schema_version == 0
    assert all(
        getattr(unknown, name).provenance is Provenance.UNAVAILABLE
        for name in unknown.REALIZED_FACT_FIELDS
    )
    assert store.list_route_observations("run-legacy") == []


def test_v43_migration_preserves_a_pre_v43_database(tmp_path: Path) -> None:
    import sqlite3

    from kew.run_store import CURRENT_SCHEMA_VERSION, SqliteRunStore

    db_path = tmp_path / "dispatch.db"
    legacy = sqlite3.connect(db_path)
    legacy.executescript(
        "CREATE TABLE schema_version (id INTEGER PRIMARY KEY, version INTEGER);"
        "INSERT INTO schema_version (id, version) VALUES (1, 42);"
        "CREATE TABLE runs ("
        "run_id TEXT PRIMARY KEY, issue_num INTEGER, branch TEXT, status TEXT,"
        "exit_code INTEGER, started_at TEXT);"
        "INSERT INTO runs VALUES ('old-run', 7, 'agent/issue-7', 'success', 0,"
        " '2026-01-01T00:00:00+00:00');"
    )
    legacy.commit()
    legacy.close()

    with SqliteRunStore(str(db_path)) as store:
        row = store._conn.execute(
            "SELECT version FROM schema_version WHERE id = 1"
        ).fetchone()
        assert row["version"] == CURRENT_SCHEMA_VERSION
        survivor = store._conn.execute(
            "SELECT run_id, branch FROM runs WHERE run_id = 'old-run'"
        ).fetchone()
        assert survivor["branch"] == "agent/issue-7"
        # No backfill: a pre-v43 attempt has no profile, and says so.
        assert store.get_route_profile("old-run", 1) is None
        assert store.route_profile_json("old-run", 1) is None
        tables = {
            r[0]
            for r in store._conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert {"run_route_profile", "run_route_observation"} <= tables


def test_host_and_contained_attempts_record_the_same_owned_facts() -> None:
    shared: dict[str, object] = {
        "source_provider": "github",
        "inference_provider": "anthropic",
        "endpoint": "https://api.anthropic.com",
        "service_tier": "priority",
        "auth_path": "env:ANTHROPIC_API_KEY",
    }
    host = compose_realized_profile(
        run_id="r",
        attempt_number=1,
        route=_route(),
        boundary=RealizedLaunchBoundary(
            containment=Containment.HOST,
            runner_digest="a" * 64,
            **shared,  # type: ignore[arg-type]
        ),
    )
    contained = compose_realized_profile(
        run_id="r",
        attempt_number=1,
        route=_route(),
        boundary=RealizedLaunchBoundary(
            containment=Containment.CONTAINED,
            image_ref="kew-agent:latest",
            image_id="sha256:" + "b" * 64,
            **shared,  # type: ignore[arg-type]
        ),
    )

    owned = (
        "source_provider",
        "inference_provider",
        "endpoint",
        "service_tier",
        "auth_path",
    )
    for name in owned:
        assert getattr(host, name) == getattr(contained, name)
    assert host.containment.value == "host"
    assert contained.containment.value == "contained"
    # Each records only the identity its boundary can own.
    assert host.image_id.provenance is Provenance.UNAVAILABLE
    assert contained.runner_digest.provenance is Provenance.UNAVAILABLE
    assert host.fingerprint != contained.fingerprint


def test_a_structurally_dishonest_profile_is_refused_before_it_is_written() -> None:
    from dataclasses import dataclass as _dataclass

    from kew.run_store_readiness import RouteProfileError, _profile_row

    @_dataclass(frozen=True)
    class _Bogus:
        run_id: str
        attempt_number: int
        fingerprint: str = "f"

        def to_json(self) -> str:
            return "{}"

        def column_values(self) -> dict[str, str | None]:
            return {"runner": "claude-code"}  # no requested_model

    with pytest.raises(RouteProfileError):
        _profile_row(_Bogus(run_id="", attempt_number=1))
    with pytest.raises(RouteProfileError):
        _profile_row(_Bogus(run_id="r", attempt_number=0))
    with pytest.raises(RouteProfileError):
        _profile_row(_Bogus(run_id="r", attempt_number=1))


def test_an_observation_requires_a_source_and_the_latest_profile_reads_back() -> None:
    store = _store()
    _admit(store, "run-f", _profile("run-f"))
    store.record_route_profile(_profile("run-f", 2, runner_digest="b" * 64))

    latest = store.get_route_profile("run-f")
    assert latest is not None
    assert latest.attempt_number == 2

    with pytest.raises(RouteProfileError):
        store.append_route_observation(
            "run-f", 2, field="reported_model", value="x", source="  "
        )
    assert store.list_route_observations("run-f") == []


def test_an_unreadable_payload_restores_as_unknown_not_as_a_reconstruction() -> None:
    unknown = RealizedRouteProfile.restore("{not json", "run-g", 2)

    assert unknown.schema_version == 0
    assert unknown.attempt_number == 2
    assert RealizedRouteProfile.restore("[1, 2]", "run-g").schema_version == 0


# --------------------------------------------------------------------------
# I05A — readiness probe persistence and honest spend accounting (Tasks 16-18)
# --------------------------------------------------------------------------


def _lease(
    store: SqliteRunStore,
    *,
    probe_id: str = "probe-1",
    fingerprint: str = "f" * 64,
    planned: float = 0.02,
    now: datetime | None = None,
    lease_ttl: timedelta | None = None,
) -> StoredReadinessProbe:
    return store.lease_readiness_probe(
        probe_id=probe_id,
        fingerprint=fingerprint,
        planned_cost=ProbeCost.planned(planned, "readiness.probe_ceiling"),
        model="claude-opus-5",
        runner="claude-code",
        now=now,
        **({} if lease_ttl is None else {"lease_ttl": lease_ttl}),
    )


def test_probe_cost_keeps_its_source_and_certainty_apart() -> None:
    planned = ProbeCost.planned(0.02, "readiness.probe_ceiling")
    observed = ProbeCost.observed(0.0134, "runner.usage")

    assert planned.certainty is Provenance.DECLARED
    assert observed.certainty is Provenance.MEASURED
    assert planned.source == "readiness.probe_ceiling"
    assert observed.amount_usd == pytest.approx(0.0134)

    unknown = ProbeCost.unknown()
    assert unknown.certainty is Provenance.UNAVAILABLE
    assert unknown.amount_usd == 0.0
    assert unknown.source == ""


def test_a_probe_cost_cannot_claim_certainty_it_does_not_have() -> None:
    with pytest.raises(ValueError):
        ProbeCost(amount_usd=0.02, source="", certainty=Provenance.DECLARED)
    with pytest.raises(ValueError):
        ProbeCost(amount_usd=-1.0, source="runner.usage", certainty=Provenance.MEASURED)
    with pytest.raises(ValueError):
        ProbeCost(amount_usd=0.02, source="x", certainty=Provenance.UNAVAILABLE)
    with pytest.raises(ValueError):
        # OBSERVED/ENFORCED describe profile facts, not money.
        ProbeCost(amount_usd=0.02, source="x", certainty=Provenance.ENFORCED)


def test_probe_cost_restores_unreadable_columns_as_unknown_not_as_zero() -> None:
    assert ProbeCost.restore(None, None, None) == ProbeCost.unknown()
    assert ProbeCost.restore(0.02, "runner.usage", "nonsense") == ProbeCost.unknown()
    assert ProbeCost.restore(0.02, "runner.usage", "measured") == ProbeCost.observed(
        0.02, "runner.usage"
    )


def test_a_lease_opens_exactly_one_spend_row_with_no_customer_identity() -> None:
    from kew.run_store import READINESS_PROBE_RUN_KIND

    store = _store()
    probe = _lease(store)

    assert probe.state == ProbeState.LEASED
    spend_rows = [r for r in store.list_all() if r.run_kind == READINESS_PROBE_RUN_KIND]
    assert len(spend_rows) == 1
    (row,) = spend_rows
    assert row.run_id == probe.run_id
    assert row.issue_num == 0
    assert row.item_id == probe.run_id  # its own fleet key, never a work item
    assert row.status == "pending"
    assert row.complexity == ""
    assert row.planned_cost_usd == pytest.approx(0.02)
    assert store.get_admission_identity(probe.run_id) is None


def test_one_fingerprint_can_only_pay_once_at_a_time() -> None:
    store = _store()
    _lease(store, probe_id="probe-1")

    with pytest.raises(ProbeLeaseHeldError):
        _lease(store, probe_id="probe-2")

    # A different exact boundary is a different lease and pays on its own.
    other = _lease(store, probe_id="probe-3", fingerprint="a" * 64)
    assert other.state == ProbeState.LEASED
    assert len(store.list_readiness_probes()) == 2


def test_settling_records_observed_cost_on_the_single_spend_row() -> None:
    store = _store()
    start = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
    probe = _lease(store, now=start)

    settled = store.settle_readiness_probe(
        probe.probe_id,
        succeeded=True,
        recorded_cost=ProbeCost.observed(0.0134, "runner.usage"),
        now=start + timedelta(seconds=12),
    )

    assert settled.state == ProbeState.SUCCEEDED
    assert settled.recorded_cost_certainty == Provenance.MEASURED
    assert settled.recorded_cost_usd == pytest.approx(0.0134)
    assert settled.recorded_cost_source == "runner.usage"
    row = store.get(probe.run_id)
    assert row is not None
    assert row.status == "success"
    assert row.estimated_cost_usd == pytest.approx(0.0134)
    assert row.finished_at is not None
    assert row.duration_seconds == pytest.approx(12.0)
    # Exactly one spend row: settling never writes a second one.
    assert len([r for r in store.list_all() if r.run_id == probe.run_id]) == 1


def test_a_failed_probe_is_evidence_and_still_costs_what_it_cost() -> None:
    store = _store()
    probe = _lease(store)
    settled = store.settle_readiness_probe(
        probe.probe_id,
        succeeded=False,
        recorded_cost=ProbeCost.observed(0.004, "runner.usage"),
        detail="provider returned 401",
    )
    assert settled.state == ProbeState.FAILED
    row = store.get(probe.run_id)
    assert row is not None
    assert row.status == "failure"
    assert row.estimated_cost_usd == pytest.approx(0.004)
    assert row.error_category == "readiness_probe_failed"


def test_exact_fingerprint_hit_miss_and_24_hour_expiry() -> None:
    store = _store()
    start = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
    probe = _lease(store, now=start)

    # An in-flight lease is not yet evidence.
    assert (
        store.lookup_readiness_probe("f" * 64, now=start).verdict is ProbeVerdict.MISS
    )

    store.settle_readiness_probe(
        probe.probe_id,
        succeeded=True,
        recorded_cost=ProbeCost.observed(0.01, "runner.usage"),
        now=start,
    )

    hit = store.lookup_readiness_probe(
        "f" * 64, now=start + timedelta(hours=23, minutes=59)
    )
    assert hit.verdict is ProbeVerdict.HIT
    assert hit.probe is not None and hit.probe.probe_id == probe.probe_id

    expired = store.lookup_readiness_probe(
        "f" * 64, now=start + timedelta(hours=24, seconds=1)
    )
    assert expired.verdict is ProbeVerdict.EXPIRED
    assert expired.probe is not None  # the row is still there, it is just stale

    # One character of boundary drift is a different probe entirely.
    assert store.lookup_readiness_probe("f" * 63 + "e", now=start).verdict is (
        ProbeVerdict.MISS
    )


def test_execution_handles_and_cleanup_acknowledgement_are_recorded() -> None:
    store = _store()
    probe = _lease(store)

    store.attach_probe_execution(
        probe.probe_id, session_id="sess-9", container_name="kew-probe-1", pgid=4242
    )
    attached = store.get_readiness_probe(probe.probe_id)
    assert attached is not None
    assert (attached.session_id, attached.container_name, attached.pgid) == (
        "sess-9",
        "kew-probe-1",
        4242,
    )
    row = store.get(probe.run_id)
    assert row is not None and row.container_name == "kew-probe-1" and row.pgid == 4242

    store.settle_readiness_probe(
        probe.probe_id,
        succeeded=True,
        recorded_cost=ProbeCost.observed(0.01, "runner.usage"),
    )
    assert [p.probe_id for p in store.list_probes_awaiting_cleanup()] == [
        probe.probe_id
    ]
    store.acknowledge_probe_cleanup(probe.probe_id)
    assert store.list_probes_awaiting_cleanup() == []
    acked = store.get_readiness_probe(probe.probe_id)
    assert acked is not None and acked.cleanup_acknowledged


def test_a_crashed_probe_reconciles_without_losing_its_planned_spend() -> None:
    store = _store()
    start = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
    probe = _lease(store, now=start, lease_ttl=timedelta(minutes=5))

    later = start + timedelta(minutes=6)
    (abandoned,) = store.reconcile_readiness_probes(now=later)

    assert abandoned.probe_id == probe.probe_id
    assert abandoned.state == ProbeState.ABANDONED
    # Spend does not disappear: the planned ceiling stands in, and says so.
    assert abandoned.recorded_cost_usd == pytest.approx(0.02)
    assert abandoned.recorded_cost_certainty == Provenance.DECLARED
    row = store.get(probe.run_id)
    assert row is not None
    assert row.status == "failure"
    assert row.error_category == "readiness_probe_abandoned"
    assert row.estimated_cost_usd == pytest.approx(0.02)
    # Cleanup was never acknowledged — that stays visible.
    assert [p.probe_id for p in store.list_probes_awaiting_cleanup()] == [
        probe.probe_id
    ]
    # And the fingerprint is leasable again.
    assert _lease(store, probe_id="probe-2", now=later).state == ProbeState.LEASED


def test_settling_a_completed_probe_with_an_unknown_cost_keeps_the_ceiling() -> None:
    """The #1556 review F1: a probe that ran and completed but whose cost is
    unknowable must not read $0 at the money row — the planned ceiling stands
    in, exactly as the reconciler does for a crashed lease, while the probe
    row keeps its honest certainty=unavailable."""
    store = _store()
    start = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
    probe = _lease(store, now=start, planned=0.02)

    settled = store.settle_readiness_probe(
        probe.probe_id,
        succeeded=True,
        recorded_cost=ProbeCost.unknown(),
        now=start + timedelta(seconds=30),
    )

    # The probe row is honest about not knowing.
    assert settled.recorded_cost_certainty == Provenance.UNAVAILABLE
    assert settled.recorded_cost_usd == 0.0
    # But the summed money row does NOT read the unknown as $0 — the ceiling
    # stands in, so spend cannot vanish at the budget/report layer.
    row = store.get(probe.run_id)
    assert row is not None
    assert row.estimated_cost_usd == pytest.approx(0.02)


def test_settling_a_known_cost_records_it_verbatim() -> None:
    """The ceiling fallback is ONLY for unknown cost — a measured cost is the
    money row exactly."""
    store = _store()
    start = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
    probe = _lease(store, now=start, planned=0.02)

    store.settle_readiness_probe(
        probe.probe_id,
        succeeded=True,
        recorded_cost=ProbeCost.observed(0.0134, "runner.usage"),
        now=start + timedelta(seconds=30),
    )

    row = store.get(probe.run_id)
    assert row is not None
    assert row.estimated_cost_usd == pytest.approx(0.0134)


def test_a_stale_lease_is_reclaimed_by_the_next_leaser() -> None:
    store = _store()
    start = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
    first = _lease(store, probe_id="probe-1", now=start, lease_ttl=timedelta(minutes=5))

    second = _lease(store, probe_id="probe-2", now=start + timedelta(minutes=6))

    assert second.state == ProbeState.LEASED
    stale = store.get_readiness_probe(first.probe_id)
    assert stale is not None and stale.state == ProbeState.ABANDONED


def test_settling_a_probe_that_is_not_leased_is_refused() -> None:
    store = _store()
    probe = _lease(store)
    store.settle_readiness_probe(
        probe.probe_id,
        succeeded=True,
        recorded_cost=ProbeCost.observed(0.01, "runner.usage"),
    )
    with pytest.raises(ProbeStateError):
        store.settle_readiness_probe(
            probe.probe_id,
            succeeded=True,
            recorded_cost=ProbeCost.observed(0.02, "runner.usage"),
        )
    with pytest.raises(ProbeStateError):
        store.settle_readiness_probe(
            "no-such-probe",
            succeeded=True,
            recorded_cost=ProbeCost.observed(0.01, "runner.usage"),
        )


def test_probe_evidence_survives_reopening_the_store(tmp_path: Path) -> None:
    db = str(tmp_path / "dispatch.db")
    start = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
    with _open(db) as store:
        probe = _lease(store, now=start)
        store.attach_probe_execution(probe.probe_id, session_id="sess-1")
        store.settle_readiness_probe(
            probe.probe_id,
            succeeded=True,
            recorded_cost=ProbeCost.observed(0.0134, "runner.usage"),
            now=start,
        )

    with _open(db) as reopened:
        found = reopened.lookup_readiness_probe("f" * 64, now=start)
        assert found.verdict is ProbeVerdict.HIT
        assert found.probe is not None
        assert found.probe.session_id == "sess-1"
        restored = ProbeCost.restore(
            found.probe.recorded_cost_usd,
            found.probe.recorded_cost_source,
            found.probe.recorded_cost_certainty,
        )
        assert restored == ProbeCost.observed(0.0134, "runner.usage")
        row = reopened.get(probe.run_id)
        assert row is not None and row.estimated_cost_usd == pytest.approx(0.0134)


def test_racing_lease_writers_admit_exactly_one_payer(tmp_path: Path) -> None:
    import threading

    db = str(tmp_path / "dispatch.db")
    with _open(db):
        pass  # migrate once, up front

    winners: list[str] = []
    refusals: list[BaseException] = []
    barrier = threading.Barrier(4)

    def _contend(n: int) -> None:
        with _open(db) as store:
            barrier.wait()
            try:
                winners.append(_lease(store, probe_id=f"probe-{n}").probe_id)
            except ProbeLeaseHeldError as exc:  # noqa: PERF203
                refusals.append(exc)

    threads = [threading.Thread(target=_contend, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(winners) == 1
    assert len(refusals) == 3
    with _open(db) as store:
        from kew.run_store import READINESS_PROBE_RUN_KIND

        rows = [r for r in store.list_all() if r.run_kind == READINESS_PROBE_RUN_KIND]
        assert len(rows) == 1  # one lease, one spend row


def test_v44_migration_preserves_a_pre_v44_database(tmp_path: Path) -> None:
    import sqlite3

    from kew.run_store import CURRENT_SCHEMA_VERSION, SqliteRunStore

    db_path = tmp_path / "dispatch.db"
    legacy = sqlite3.connect(db_path)
    legacy.executescript(
        "CREATE TABLE schema_version (id INTEGER PRIMARY KEY, version INTEGER);"
        "INSERT INTO schema_version (id, version) VALUES (1, 43);"
        "CREATE TABLE runs ("
        "run_id TEXT PRIMARY KEY, issue_num INTEGER, branch TEXT, status TEXT,"
        "exit_code INTEGER, started_at TEXT);"
        "INSERT INTO runs VALUES ('old-run', 7, 'agent/issue-7', 'success', 0,"
        " '2026-01-01T00:00:00+00:00');"
    )
    legacy.commit()
    legacy.close()

    with SqliteRunStore(str(db_path)) as store:
        row = store._conn.execute(
            "SELECT version FROM schema_version WHERE id = 1"
        ).fetchone()
        assert row["version"] == CURRENT_SCHEMA_VERSION
        survivor = store._conn.execute(
            "SELECT run_id, branch FROM runs WHERE run_id = 'old-run'"
        ).fetchone()
        assert survivor["branch"] == "agent/issue-7"
        assert store.list_readiness_probes() == []
        tables = {
            r[0]
            for r in store._conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert "readiness_probe" in tables


def _open(db: str) -> SqliteRunStore:
    from kew.run_store import SqliteRunStore

    return SqliteRunStore(db)


def test_a_settling_probe_and_a_racing_reconciler_agree_on_one_spend(
    tmp_path: Path,
) -> None:
    """Task 18: whoever wins the race, the probe has ONE final state and ONE
    amount on its one spend row — never two writes, never zero."""
    import threading

    db = str(tmp_path / "dispatch.db")
    start = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
    late = start + timedelta(minutes=30)
    with _open(db) as seed:
        probe = _lease(seed, now=start, lease_ttl=timedelta(minutes=5))

    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def _settle() -> None:
        with _open(db) as store:
            barrier.wait()
            try:
                store.settle_readiness_probe(
                    probe.probe_id,
                    succeeded=True,
                    recorded_cost=ProbeCost.observed(0.0134, "runner.usage"),
                    now=late,
                )
                outcomes.append("settled")
            except ProbeStateError:
                outcomes.append("lost")

    def _reconcile() -> None:
        with _open(db) as store:
            barrier.wait()
            outcomes.append(
                "abandoned" if store.reconcile_readiness_probes(now=late) else "no-op"
            )

    threads = [threading.Thread(target=_settle), threading.Thread(target=_reconcile)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    with _open(db) as store:
        final = store.get_readiness_probe(probe.probe_id)
        row = store.get(probe.run_id)
        assert final is not None and row is not None
        assert final.state in (ProbeState.SUCCEEDED, ProbeState.ABANDONED)
        assert row.estimated_cost_usd == pytest.approx(final.recorded_cost_usd)
        assert row.status in ("success", "failure")
        assert len([r for r in store.list_all() if r.run_id == probe.run_id]) == 1
    assert sorted(outcomes) in (["no-op", "settled"], ["abandoned", "lost"])


def test_reconciling_a_probe_never_touches_a_dispatch_row(tmp_path: Path) -> None:
    """Task 18: probe terminalization is scoped to its own run kind."""
    from dataclasses import asdict

    from kew.run_store import RunRecord

    start = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
    with _open(str(tmp_path / "dispatch.db")) as store:
        work = RunRecord(
            run_id="work-1",
            issue_num=7,
            item_id="7",
            branch="agent/issue-7",
            status="pending",
            exit_code=-1,
            started_at=start,
            estimated_cost_usd=1.25,
        )
        store.save(work)
        before = asdict(store.get("work-1"))  # type: ignore[arg-type]

        _lease(store, now=start, lease_ttl=timedelta(minutes=5))
        assert store.reconcile_readiness_probes(now=start + timedelta(hours=1))

        assert asdict(store.get("work-1")) == before  # type: ignore[arg-type]
