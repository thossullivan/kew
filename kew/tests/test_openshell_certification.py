"""The OpenShell egress certification, and the admission gate that needs it.

These tests are the SC-0 packet-certification tests (``tests/test_audit.py``)
carried over to the runtime-neutral proof: a pass, a failure on every single
reachable denied target, a failure without a reachable positive control,
expiry, and a mismatch on each of the four bound facts. The matrix differs and
the tuple differs; the property under test does not.

One test here exists only because of how SC-0 went wrong: its writer had no
production caller at all, so nothing on a real host ever wrote the proof its
admission gate demanded. ``test_the_certify_path_appends_through_the_audit_writer``
drives the shipped command's own code path and requires the event to land, and
``test_the_audit_writer_has_a_production_caller`` reads the source tree so a
later refactor cannot quietly orphan it again.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from support.gh_fakes import install_placeholder_forge
from support.image_resolution import built_by_kew

from kew.run_store import SqliteRunStore

_REPO_ROOT = Path(__file__).resolve().parents[2]

_IMAGE_ID = "sha256:" + "a" * 64
_POLICY_SHA = "b" * 64
_SUPERVISOR = "c" * 64
_GATEWAY = "0.1.2"
_GATEWAY_ID = "d" * 64


@pytest.fixture(autouse=True)
def _placeholder_forge(monkeypatch: pytest.MonkeyPatch) -> None:
    """No ``gh`` call reaches GitHub: the forge answers as ``o/r`` does (#3069)."""
    install_placeholder_forge(monkeypatch)


def _binding(**overrides: str) -> dict[str, str]:
    bound = {
        "image_id": _IMAGE_ID,
        "policy_sha256": _POLICY_SHA,
        "gateway_version": _GATEWAY,
        "gateway_identity": _GATEWAY_ID,
        "supervisor_digest": _SUPERVISOR,
    }
    bound.update(overrides)
    return bound


def _evidence(
    *,
    control: str = "reachable",
    leaks: tuple[str, ...] = (),
    unavailable: tuple[str, ...] = (),
    controls: dict[str, str] | None = None,
) -> Any:
    """One matrix, with every same-protocol control the contract requires.

    ``controls`` overrides a single leg's own control; by default each tracks
    the shared one, which is what a sandbox whose UDP and IPv6 both work to the
    allowed destinations reports.
    """
    from kew.egress_certification import (
        OPENSHELL_PACKET_CONTROLS,
        OPENSHELL_PACKET_DENIALS,
        build_packet_evidence,
    )
    from kew.readiness import PacketProbeStatus

    observations = {}
    for name in OPENSHELL_PACKET_DENIALS:
        if name in leaks:
            observations[name] = PacketProbeStatus.REACHABLE
        elif name in unavailable:
            observations[name] = PacketProbeStatus.UNAVAILABLE
        else:
            observations[name] = PacketProbeStatus.DENIED
    overrides = controls or {}
    return build_packet_evidence(
        positive_control=PacketProbeStatus(control),
        observations=observations,
        required=OPENSHELL_PACKET_DENIALS,
        controls={
            name: PacketProbeStatus(overrides.get(name, control))
            for name in OPENSHELL_PACKET_CONTROLS
        },
    )


def _certification(**overrides: Any) -> Any:
    from kew.egress_certification import (
        CERTIFICATION_SCHEMA_VERSION,
        RuntimeEgressCertification,
    )
    from kew.runtime_names import OPENSHELL_RUNTIME

    fields: dict[str, Any] = {
        "schema_version": CERTIFICATION_SCHEMA_VERSION,
        "runtime": OPENSHELL_RUNTIME,
        "packets": (("/usr/bin/node", _evidence()),),
        **_binding(),
    }
    # ``packet`` is the one-binary spelling most tests need.
    if "packet" in overrides:
        fields["packets"] = (("/usr/bin/node", overrides.pop("packet")),)
    fields.update(overrides)
    return RuntimeEgressCertification(**fields)


def _actor() -> Any:
    from kew.audit import egress_certification_actor
    from kew.runtime_names import OPENSHELL_RUNTIME

    return egress_certification_actor(OPENSHELL_RUNTIME)


def _match(store: SqliteRunStore, **overrides: str) -> Any:
    from kew.audit import matching_runtime_egress_certification
    from kew.runtime_names import OPENSHELL_RUNTIME

    return matching_runtime_egress_certification(
        store, runtime=OPENSHELL_RUNTIME, **_binding(**overrides)
    )


# ---------------------------------------------------------------------------
# The proof itself
# ---------------------------------------------------------------------------


def test_a_complete_passing_matrix_is_recorded_and_matched(tmp_path: Path) -> None:
    """The one shape that certifies: control reachable, every target denied."""
    from kew.audit import append_runtime_egress_certification
    from kew.audit_chain import verify_chain

    certification = _certification()
    assert certification.verdict == "passed"
    with SqliteRunStore(str(tmp_path / "certify.db")) as store:
        event_id = append_runtime_egress_certification(
            store, certification, actor=_actor()
        )
        assert event_id >= 1
        assert _match(store) == certification
        assert verify_chain(store.read_chain_rows()).ok


@pytest.mark.parametrize(
    "leak",
    [
        "external_host",
        "public_ip",
        "udp_dns",
        "ipv6_external",
        "link_local_metadata",
        "host_gateway",
        "github",
        "unresolvable_name",
    ],
)
def test_any_single_reachable_denied_target_fails_the_certification(
    tmp_path: Path, leak: str
) -> None:
    """One leak is a failed boundary — and an unwritable certification.

    Parametrized over the whole matrix on purpose: a verdict computed from
    ``any``/``all`` over the wrong collection passes seven of these and fails
    one, which a single-case test would not see.
    """
    from kew.audit import append_runtime_egress_certification

    certification = _certification(packet=_evidence(leaks=(leak,)))
    assert certification.verdict == "failed"
    failing = [
        outcome for outcome in certification.packets[0][1].denials if not outcome.passed
    ]
    assert [outcome.name for outcome in failing] == [leak]
    assert failing[0].reason == "packet_leak"
    with SqliteRunStore(str(tmp_path / "leak.db")) as store:
        with pytest.raises(ValueError, match="did not pass"):
            append_runtime_egress_certification(store, certification, actor=_actor())
        assert store.list_audit_events(limit=None) == []


@pytest.mark.parametrize(
    ("control", "verdict", "reason"),
    [
        ("denied", "failed", "positive_control_failed"),
        ("unavailable", "unavailable", "positive_control_unavailable"),
    ],
)
def test_a_matrix_without_a_reachable_positive_control_cannot_certify(
    tmp_path: Path, control: str, verdict: str, reason: str
) -> None:
    """A sandbox with no working egress denies everything and proves nothing.

    This is the false pass the positive control exists to stop: without it a
    matrix of eight denials reads identically whether the policy closed the
    boundary or the sandbox simply had no network.
    """
    from kew.audit import append_runtime_egress_certification

    certification = _certification(packet=_evidence(control=control))
    assert certification.verdict == verdict
    assert {outcome.reason for outcome in certification.packets[0][1].denials} == {
        reason
    }
    assert not any(outcome.passed for outcome in certification.packets[0][1].denials)
    with (
        SqliteRunStore(str(tmp_path / "control.db")) as store,
        pytest.raises(ValueError, match="did not pass"),
    ):
        append_runtime_egress_certification(store, certification, actor=_actor())


@pytest.mark.parametrize("leg", ["udp_dns", "ipv6_external"])
@pytest.mark.parametrize(
    ("control", "verdict", "reason"),
    [
        ("denied", "failed", "positive_control_failed"),
        ("unavailable", "unavailable", "positive_control_unavailable"),
    ],
)
def test_a_protocol_leg_without_its_own_control_cannot_certify(
    leg: str, control: str, verdict: str, reason: str
) -> None:
    """A sandbox with no UDP path or no IPv6 route proves nothing about either.

    The shared positive control is one TLS connection over TCP and IPv4. A
    sandbox with no IPv6 route, or no UDP path at all, denies every IPv6 and
    every UDP destination whatever the policy enforces — so without a
    same-protocol control these two legs recorded ``denied`` on an untested
    protocol and the certification passed (#2389 review). kew now refuses the
    boundary rather than certifying the leg.
    """
    certification = _certification(packet=_evidence(controls={leg: control}))
    assert certification.verdict == verdict
    failing = {
        outcome.name: outcome.reason
        for outcome in certification.packets[0][1].denials
        if not outcome.passed
    }
    assert failing == {leg: reason}
    # Every other leg still passes on the shared control it was observed beside.
    assert all(
        outcome.positive_control.value == "reachable"
        for outcome in certification.packets[0][1].denials
        if outcome.name != leg
    )


def test_the_control_contract_is_the_runtimes_own_not_the_payloads() -> None:
    """A stored proof cannot widen the set of legs that supply their control.

    Otherwise a forged certification could claim ``github`` carried a control
    of its own, pass that leg on a value nothing ran, and keep the matrix
    internally consistent.
    """
    from kew.egress_certification import (
        OPENSHELL_PACKET_CONTROLS,
        OPENSHELL_PACKET_DENIALS,
        RuntimeEgressCertification,
        build_packet_evidence,
    )
    from kew.readiness import PacketProbeStatus

    widened = build_packet_evidence(
        positive_control=PacketProbeStatus.REACHABLE,
        observations={n: PacketProbeStatus.DENIED for n in OPENSHELL_PACKET_DENIALS},
        required=OPENSHELL_PACKET_DENIALS,
        controls={
            **{n: PacketProbeStatus.REACHABLE for n in OPENSHELL_PACKET_CONTROLS},
            "github": PacketProbeStatus.REACHABLE,
        },
    )
    with pytest.raises(ValueError, match="control contract is wrong"):
        _certification(packet=widened)
    # And a matrix that supplied none of them is equally not the contract.
    narrowed = build_packet_evidence(
        positive_control=PacketProbeStatus.REACHABLE,
        observations={n: PacketProbeStatus.DENIED for n in OPENSHELL_PACKET_DENIALS},
        required=OPENSHELL_PACKET_DENIALS,
    )
    with pytest.raises(ValueError, match="control contract is wrong"):
        _certification(packet=narrowed)
    # The payload carries each leg's control, and the reader rebuilds it under
    # the RUNTIME's contract rather than anything the payload claims.
    certification = _certification()
    assert RuntimeEgressCertification.from_json(certification.to_json()) == (
        certification
    )
    assert certification.packets[0][1].protocol_controls == OPENSHELL_PACKET_CONTROLS


def test_a_probe_that_reports_no_same_protocol_control_is_refused() -> None:
    """A result line without the controls never falls back to the shared one."""
    import json

    from kew.openshell_egress import (
        PROBE_RESULT_SCHEMA,
        RESULT_MARKER,
        EgressProbeError,
        parse_probe_output,
    )

    payload = json.loads(_probe_output()[len(RESULT_MARKER) :])
    control, _observations, controls, _details = parse_probe_output(_probe_output())
    assert control.value == "reachable"
    assert set(controls) == {"udp_dns", "ipv6_external"}
    payload.pop("controls")
    with pytest.raises(EgressProbeError, match="no same-protocol control"):
        parse_probe_output(RESULT_MARKER + json.dumps(payload))
    payload["controls"] = {"udp_dns": "reachable"}
    with pytest.raises(EgressProbeError, match="no same-protocol control"):
        parse_probe_output(RESULT_MARKER + json.dumps(payload))
    assert payload["schema"] == PROBE_RESULT_SCHEMA


def test_the_probe_script_runs_a_same_protocol_control_per_protocol_leg() -> None:
    """The inline script must actually dial each leg's own protocol.

    Asserted on the script text because the probe body only ever runs inside a
    sandbox on an operator's host: nothing in this suite can execute it, so the
    one thing a test can hold is that the controls are wired and keyed to the
    legs they govern.
    """
    from kew.openshell_egress import _PROBE_SCRIPT

    assert "['udp_dns', udpControl()]" in _PROBE_SCRIPT
    assert "['ipv6_external', ipv6Control(params.inference_host)]" in _PROBE_SCRIPT
    # The UDP control uses the sandbox's own resolver, and the IPv6 control the
    # allowed host over IPv6 only — not the shared control's IPv4 connection.
    assert "dns.getServers()" in _PROBE_SCRIPT
    assert "tcpProbe(host, 443, true, 6)" in _PROBE_SCRIPT
    # The denied IPv6 leg is dialled over IPv6 too, not whatever resolves first.
    assert "tcpProbe(params.ipv6_host, 443, false, 6)" in _PROBE_SCRIPT


def test_the_probe_script_never_writes_its_own_schema_number() -> None:
    """The wire version has ONE definition, and the script echoes it back.

    Written as a literal on both sides, ``PROBE_RESULT_SCHEMA`` could be bumped
    with the inline script left behind. Nothing in this suite can execute the
    probe — it only ever runs inside a sandbox on an operator's host — so the
    whole suite stayed green while every real ``kew sandbox certify`` run would
    have refused its own result as "unknown schema" (#2389 review). The script
    is handed the number in :func:`probe_parameters` instead, so there is no
    second copy to leave behind.
    """
    import re

    from kew.openshell_egress import (
        _PROBE_SCRIPT,
        PROBE_RESULT_SCHEMA,
        probe_parameters,
    )

    assert re.search(r"schema:\s*[0-9]", _PROBE_SCRIPT) is None
    assert "const SCHEMA = params.schema;" in _PROBE_SCRIPT
    # Both result lines — the verdict and the probe's own error report — are
    # read by `_probe_result_payload`, so both have to carry the live number.
    assert _PROBE_SCRIPT.count("schema: SCHEMA,") == 2
    parameters = probe_parameters(
        inference_host="api.anthropic.com",
        gateway_port=8080,
        forge_host="ghe.example.test",
    )
    assert parameters["schema"] == PROBE_RESULT_SCHEMA


def test_an_unavailable_probe_is_not_a_denial() -> None:
    """ "kew could not tell" never shortens to a closed boundary."""
    certification = _certification(packet=_evidence(unavailable=("udp_dns",)))
    assert certification.verdict == "unavailable"


def test_an_incomplete_matrix_is_refused_rather_than_certified() -> None:
    """A boundary is not partially closed, so a short matrix is invalid."""
    from kew.egress_certification import (
        OPENSHELL_PACKET_DENIALS,
        EgressPacketEvidence,
        build_packet_evidence,
    )
    from kew.readiness import PacketProbeStatus

    partial = {name: PacketProbeStatus.DENIED for name in OPENSHELL_PACKET_DENIALS[:-1]}
    with pytest.raises(ValueError, match=OPENSHELL_PACKET_DENIALS[-1]):
        build_packet_evidence(
            positive_control=PacketProbeStatus.REACHABLE,
            observations=partial,
            required=OPENSHELL_PACKET_DENIALS,
        )
    with pytest.raises(ValueError, match="unknown targets: invented"):
        build_packet_evidence(
            positive_control=PacketProbeStatus.REACHABLE,
            observations={
                **{n: PacketProbeStatus.DENIED for n in OPENSHELL_PACKET_DENIALS},
                "invented": PacketProbeStatus.DENIED,
            },
            required=OPENSHELL_PACKET_DENIALS,
        )
    # A matrix that is internally consistent but not this runtime's contract.
    short = EgressPacketEvidence(
        positive_control=PacketProbeStatus.REACHABLE,
        denials=_evidence().denials[:-1],
    )
    with pytest.raises(ValueError, match="complete matrix"):
        _certification(packet=short)


def test_each_outcome_must_carry_the_control_it_was_observed_beside() -> None:
    """A denial cannot claim a positive control that was never run."""
    from kew.egress_certification import EgressPacketEvidence
    from kew.readiness import PacketProbeStatus, evaluate_topology_denial

    forged = evaluate_topology_denial(
        name="github",
        positive_control=PacketProbeStatus.REACHABLE,
        worker=PacketProbeStatus.DENIED,
    )
    with pytest.raises(ValueError, match="disagrees with the positive control"):
        EgressPacketEvidence(
            positive_control=PacketProbeStatus.UNAVAILABLE, denials=(forged,)
        )


def test_the_certification_round_trips_through_its_payload() -> None:
    """What admission reads back is what the prober recorded, byte for byte."""
    from kew.egress_certification import RuntimeEgressCertification

    certification = _certification()
    assert RuntimeEgressCertification.from_json(certification.to_json()) == (
        certification
    )
    payload = certification.to_dict()
    assert payload["packet_verdict"] == "passed"
    payload["packet_verdict"] = "failed"
    with pytest.raises(ValueError, match="verdict is inconsistent"):
        RuntimeEgressCertification.from_dict(payload)


@pytest.mark.parametrize(
    "unsupported", ["docker", "podman", "", "OPENSHELL", "firecracker"]
)
def test_only_a_runtime_with_a_contract_can_be_certified(unsupported: str) -> None:
    """No contract means no certification — never an empty matrix that passes."""
    with pytest.raises(ValueError, match="no egress certification contract"):
        _certification(runtime=unsupported)


# ---------------------------------------------------------------------------
# Matching: freshness, the exact tuple, and every unreadable case
# ---------------------------------------------------------------------------


#: Each way of not having the proof, and the diagnosis the matcher must give
#: for it. The exception type is the same for all of them — unavailable
#: evidence is treated exactly like absent evidence — but the CAUSE has to be
#: the right one, and asserting it is what makes these legs bite.
#:
#: Two legs were vacuous without it (#2389 review). ``runtime`` substitutes a
#: runtime with no certification contract, so dropping the runtime comparison
#: from the admission tuple still refuses — as "malformed", from the schema
#: check, rather than as a boundary mismatch. ``conflict`` pairs a passing
#: proof with a failing one, and the "did not pass" guard happens to catch
#: whichever of the two the bounded lookup returns first, so dropping the
#: conflict guard left the whole suite green.
#:
#: VOLUME is deliberately not a leg here. "65 valid proofs in one window" was
#: one, and it was wrong: see
#: ``test_a_host_that_certifies_often_is_not_refused_for_certifying_often``.
_UNMATCHED_LEGS: tuple[tuple[str, str], ...] = (
    ("conflict", "conflicts"),
    ("stale", "matches this boundary, or it expired"),
    ("future", "time is invalid"),
    ("malformed", "is malformed"),
    ("owner", "owner is invalid"),
    ("missing", "matches this boundary, or it expired"),
    ("image_id", "matches this boundary, or it expired"),
    ("policy_sha256", "matches this boundary, or it expired"),
    ("gateway_version", "matches this boundary, or it expired"),
    ("supervisor_digest", "matches this boundary, or it expired"),
    ("runtime", "matches this boundary, or it expired"),
)


@pytest.mark.parametrize(("invalid", "cause"), _UNMATCHED_LEGS)
def test_unmatched_expired_or_conflicting_proof_cannot_authorize(
    tmp_path: Path, invalid: str, cause: str
) -> None:
    """Every way of not having the proof refuses identically — and says why.

    One exception type, eleven causes. The cause is asserted because three of
    these legs refuse for a reason that is NOT the rule under test: a
    well-formed payload bound to the wrong image, a runtime with no contract,
    and a conflicting pair all reach a later guard that also raises. Without
    the cause, removing the rule the leg names leaves the leg green.
    """
    from dataclasses import replace

    from kew.audit import (
        RUNTIME_EGRESS_CERTIFICATION_EVENT,
        EgressCertificationUnavailable,
        append_runtime_egress_certification,
    )
    from kew.readiness import PacketProbeStatus

    certification = _certification()
    failed = replace(
        certification,
        packets=(("/usr/bin/node", _evidence(control=PacketProbeStatus.DENIED.value)),),
    )
    with SqliteRunStore(str(tmp_path / "match.db")) as store:
        if invalid == "conflict":
            append_runtime_egress_certification(store, certification, actor=_actor())
        timestamp = datetime.now(UTC)
        payload = failed.to_dict() if invalid == "conflict" else certification.to_dict()
        if invalid == "stale":
            timestamp -= timedelta(hours=25)
        if invalid == "future":
            timestamp += timedelta(minutes=1)
        if invalid == "malformed":
            payload["unexpected"] = "field"
        if invalid in {
            "image_id",
            "policy_sha256",
            "gateway_version",
            "supervisor_digest",
        }:
            # Each substitute is WELL FORMED for its field and merely different.
            # `image_id` used to be a bare 64-hex digest, which `_IMAGE_ID_RE`
            # rejects at `from_dict` — so the leg refused on the schema whether
            # or not image_id was in the admission tuple at all (#2389 review).
            payload[invalid] = {
                "image_id": "sha256:" + "9" * 64,
                "policy_sha256": "9" * 64,
                "gateway_version": "9.9.9",
                "supervisor_digest": "9" * 64,
            }[invalid]
        if invalid == "runtime":
            payload["runtime"] = "docker"
        if invalid != "missing":
            store.append_audit_event(
                timestamp=timestamp.isoformat(),
                event=RUNTIME_EGRESS_CERTIFICATION_EVENT,
                actor_type="policy",
                actor_id="wrong-owner" if invalid == "owner" else _actor().id,
                payload=payload,
            )
        with pytest.raises(EgressCertificationUnavailable, match=cause):
            _match(store)


def test_a_host_that_certifies_often_is_not_refused_for_certifying_often(
    tmp_path: Path,
) -> None:
    """Valid proof must never accumulate into a refusal (#2389 review).

    ``kew sandbox certify`` is a shipped command, so proofs pile up from
    ordinary use. The lookup used to stop at one page of 65 and refuse when it
    filled, so 65 good certifications inside the same 24-hour window refused
    every OpenShell dispatch — and the refusal printed "run certify again",
    which appended the 66th. The window is paged now, so more proof is only
    ever more proof.
    """
    from kew.audit import (
        _EVIDENCE_PAGE,
        RUNTIME_EGRESS_CERTIFICATION_EVENT,
        append_runtime_egress_certification,
    )

    certification = _certification()
    with SqliteRunStore(str(tmp_path / "many.db")) as store:
        for _ in range(_EVIDENCE_PAGE * 2):
            append_runtime_egress_certification(store, certification, actor=_actor())
        # Non-vacuous: the writer must really have left more than one page
        # behind, or this would pass without paging anything.
        appended = store.list_audit_events(
            event=RUNTIME_EGRESS_CERTIFICATION_EVENT, limit=None
        )
        assert len(appended) > _EVIDENCE_PAGE
        assert _match(store) == certification


def test_the_paged_lookup_still_refuses_a_ledger_it_cannot_finish_reading() -> None:
    """Paging is not unbounded: the hard cap is still a refusal, not a pass.

    A matching proof sits in every page here, so a cap that quietly returned
    what it had seen would authorize. It must not: the whole point of reading
    the window out is that an older CONFLICTING proof cannot hide behind newer
    ones, and a scan that never reached the end has not shown that.
    """
    from typing import Any as _Any

    from kew.audit import (
        _EVIDENCE_PAGE,
        _EVIDENCE_SCAN_CAP,
        EgressCertificationUnavailable,
        matching_runtime_egress_certification,
    )
    from kew.run_store import AuditEvent
    from kew.runtime_names import OPENSHELL_RUNTIME

    payload = _certification().to_dict()
    reads = 0

    class _EndlessLedger:
        """A ledger whose matching certifications never run out."""

        def list_audit_events(
            self, limit: int | None = None, *, after_id: int | None = None, **kw: _Any
        ) -> list[AuditEvent]:
            nonlocal reads
            reads += 1
            assert limit is not None, "the lookup must stay bounded per read"
            top = (after_id - 1) if after_id is not None else _EVIDENCE_SCAN_CAP * 10
            return [
                AuditEvent(
                    id=top - offset,
                    timestamp=datetime.now(UTC).isoformat(),
                    event=kw["event"],
                    actor_type="policy",
                    actor_id=_actor().id,
                    payload=dict(payload),
                )
                for offset in range(limit)
            ]

    with pytest.raises(EgressCertificationUnavailable, match="bounded lookup"):
        matching_runtime_egress_certification(
            _EndlessLedger(),  # type: ignore[arg-type]
            runtime=OPENSHELL_RUNTIME,
            **_binding(),
        )
    assert reads <= -(-_EVIDENCE_SCAN_CAP // _EVIDENCE_PAGE) + 1


def test_a_boundary_that_both_passed_and_failed_is_a_conflict(tmp_path: Path) -> None:
    """Two proofs that disagree are unavailable evidence, in EITHER order.

    The order matters because the "did not pass" guard only inspects the first
    match the bounded lookup returns: with the passing proof first, dropping
    the conflict guard would hand admission a pass while the ledger also holds
    a failure for the same boundary tuple. Both orders are asserted so neither
    one of the two guards can stand in for the other.
    """
    from dataclasses import replace

    from kew.audit import (
        RUNTIME_EGRESS_CERTIFICATION_EVENT,
        EgressCertificationUnavailable,
    )
    from kew.readiness import PacketProbeStatus

    passing = _certification().to_dict()
    failing = replace(
        _certification(),
        packets=(("/usr/bin/node", _evidence(control=PacketProbeStatus.DENIED.value)),),
    ).to_dict()
    for index, order in enumerate(((passing, failing), (failing, passing))):
        with SqliteRunStore(str(tmp_path / f"conflict{index}.db")) as store:
            for payload in order:
                store.append_audit_event(
                    timestamp=datetime.now(UTC).isoformat(),
                    event=RUNTIME_EGRESS_CERTIFICATION_EVENT,
                    actor_type="policy",
                    actor_id=_actor().id,
                    payload=payload,
                )
            with pytest.raises(EgressCertificationUnavailable, match="conflicts"):
                _match(store)


def test_a_rerun_renews_an_expired_certification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Expiry is about the clock, not about the chain: a real rerun re-admits."""
    import kew.audit as audit

    certification = _certification()
    observed_at = datetime.now(UTC) - timedelta(hours=25)

    class Clock(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:
            return observed_at

    monkeypatch.setattr(audit, "datetime", Clock)
    with SqliteRunStore(str(tmp_path / "renew.db")) as store:
        audit.append_runtime_egress_certification(store, certification, actor=_actor())
        observed_at += timedelta(hours=25)
        with pytest.raises(audit.EgressCertificationUnavailable):
            _match(store)
        audit.append_runtime_egress_certification(store, certification, actor=_actor())
        assert _match(store) == certification


def test_the_writer_refuses_an_actor_that_is_not_the_gate(tmp_path: Path) -> None:
    """One signing identity, derived from the runtime, enforced on write."""
    from kew.audit import Actor, append_runtime_egress_certification

    with SqliteRunStore(str(tmp_path / "actor.db")) as store:
        with pytest.raises(ValueError, match="actor is invalid"):
            append_runtime_egress_certification(
                store, _certification(), actor=Actor("policy", "sidecar-packet-gate")
            )
        with pytest.raises(ValueError, match="actor is invalid"):
            append_runtime_egress_certification(
                store, _certification(), actor=Actor("human", "openshell-egress-gate")
            )


def test_a_store_that_cannot_answer_is_unavailable_not_absent(
    tmp_path: Path,
) -> None:
    """A read failure is never reported as "no certification needed"."""
    from kew.audit import EgressCertificationUnavailable

    class Broken:
        def list_audit_events(self, **_: Any) -> list[Any]:
            raise OSError("ledger unreadable")

    with pytest.raises(EgressCertificationUnavailable, match="unavailable"):
        _match(Broken())  # type: ignore[arg-type]
    assert tmp_path.is_dir()


# ---------------------------------------------------------------------------
# The production certify path
# ---------------------------------------------------------------------------


class _FakeExec:
    def __init__(self, output: str) -> None:
        self.drained = [output]
        self.waited: float | None = None

    def wait(self, timeout: float) -> int:
        self.waited = timeout
        return 0


class _FakeClient:
    """Stands in for the gateway, recording the calls the prober makes."""

    def __init__(
        self,
        output: str | Any,
        *,
        delete_error: Exception | None = None,
        endpoint: str = "http://127.0.0.1:8080",
        sandbox_image: str = "openshell-sandbox@sha256:" + "e" * 64,
    ) -> None:
        #: A probe result, or a callable of the policy bytes the probe sandbox
        #: was created under — which is how a test makes one clause leak.
        self.output = output
        self.delete_error = delete_error
        self.created: list[dict[str, Any]] = []
        self.deleted: list[str] = []
        self.argv: list[str] = []
        self.sandbox_image = sandbox_image
        from kew.openshell import Gateway

        self.gateway = Gateway(endpoint, "config")

    def check_version(self) -> str:
        return _GATEWAY

    def report(self) -> Any:
        from kew.openshell import GatewayReport

        return GatewayReport(
            gateway=self.gateway,
            version=_GATEWAY,
            tls=True,
            telemetry=False,
            agent_proposals=False,
            agent_approval="manual",
            supervisor_image="openshell-supervisor@sha256:" + "f" * 64,
            sandbox_image=self.sandbox_image,
            landlock_abi=3,
            raw={"gateway": self.gateway.endpoint},
        )

    def sandbox_create(self, name: str, **kwargs: Any) -> None:
        # Read NOW: the file is a private copy that is gone once create returns.
        policy = kwargs["policy_path"].read_bytes()
        self.created.append({"name": name, "policy": policy, **kwargs})

    def sandbox_exec(self, name: str, *, workdir: str, argv: list[str]) -> _FakeExec:
        self.argv = list(argv)
        output = self.output
        if callable(output):
            output = output(self.created[-1]["policy"])
        return _FakeExec(output)

    def sandbox_delete(self, name: str) -> None:
        self.deleted.append(name)
        if self.delete_error is not None:
            raise self.delete_error


def _probe_output(
    *,
    controls: dict[str, str] | None = None,
    launcher: str | None = None,
    **overrides: str,
) -> str:
    """One probe result line, in the wire format the inline script writes.

    ``controls`` sets a leg's own same-protocol control; by default both
    report ``reachable``, which is a sandbox with a working UDP path and a
    working IPv6 route to the allowed host.
    """
    import json

    from kew.egress_certification import (
        OPENSHELL_PACKET_CONTROLS,
        OPENSHELL_PACKET_DENIALS,
    )
    from kew.openshell_egress import (
        PROBE_RESULT_SCHEMA,
        RESULT_MARKER,
        openshell_supervisor_digest,
    )

    targets = {name: "denied" for name in OPENSHELL_PACKET_DENIALS}
    control = overrides.pop("positive_control", "reachable")
    targets.update(overrides)
    observed = {
        name: (controls or {}).get(name, "reachable")
        for name in OPENSHELL_PACKET_CONTROLS
    }
    return RESULT_MARKER + json.dumps(
        {
            "schema": PROBE_RESULT_SCHEMA,
            "positive_control": control,
            "controls": observed,
            "targets": targets,
            "details": {
                name: "probe detail"
                for name in (
                    "positive_control",
                    *targets,
                    *(f"{name}_control" for name in observed),
                )
            },
            # The image under test carries this kew's own launcher unless a
            # test says otherwise.
            "launcher_sha256": (
                openshell_supervisor_digest() if launcher is None else launcher
            ),
        }
    )


@pytest.fixture
def certify_host(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Patch only the gateway and the image store; the rest is production."""
    import kew.openshell_egress as openshell_egress
    import kew.sandbox as sandbox
    from kew.config import DispatchConfig

    monkeypatch.setattr(
        sandbox,
        "inspect_image_id",
        lambda runtime, image: built_by_kew(image, _IMAGE_ID),
    )

    def install(client: _FakeClient) -> DispatchConfig:
        monkeypatch.setattr(
            openshell_egress,
            "OpenShellClient",
            type("Stub", (), {"from_settings": staticmethod(lambda _s: client)}),
        )
        config = DispatchConfig()
        config.sandbox.runtime = "openshell"
        config.sandbox.images = {"claude-code": "kew-agent:docker-000000000000"}
        return config

    return install


def test_the_certify_path_appends_through_the_audit_writer(
    tmp_path: Path, certify_host: Any
) -> None:
    """The gap SC-0 left: the shipped command must write the proof it demands.

    SC-0's certification writer had only test callers, so no operator could
    ever satisfy the gate that required its output. This drives the function
    ``kew sandbox certify`` calls and requires the event to land on the chain
    and to match through the same reader admission uses.
    """
    from kew.audit import RUNTIME_EGRESS_CERTIFICATION_EVENT
    from kew.audit_chain import verify_chain
    from kew.openshell_egress import certify_openshell_egress

    client = _FakeClient(_probe_output())
    config = certify_host(client)
    with SqliteRunStore(str(tmp_path / "certify.db")) as store:
        result = certify_openshell_egress(config, runner="claude-code", store=store)
        assert result.passed
        assert result.event_id is not None
        [event] = store.list_audit_events(
            event=RUNTIME_EGRESS_CERTIFICATION_EVENT, limit=None
        )
        assert event.payload["runtime"] == "openshell"
        assert event.payload["image_id"] == _IMAGE_ID
        assert event.payload["packet_verdict"] == "passed"
        assert event.actor_id == _actor().id
        assert verify_chain(store.read_chain_rows()).ok
        matched = _match(
            store,
            policy_sha256=result.certification.policy_sha256,
            gateway_identity=result.certification.gateway_identity,
            supervisor_digest=result.certification.supervisor_digest,
        )
        assert matched == result.certification
    # One probe sandbox per governed binary, each created from the IMMUTABLE
    # image ID under that binary's own clause, and every one deleted.
    assert [created["image"] for created in client.created] == [_IMAGE_ID] * 3
    assert [created["policy"] for created in client.created] == _probe_policies(
        "claude-code"
    )
    assert client.deleted == [created["name"] for created in client.created]
    assert result.certification.binaries == tuple(
        binary["path"] for binary in _policy("claude-code")["binaries"]
    )
    assert result.cleanup_error is None


@pytest.mark.policy
def test_the_audit_writer_has_a_production_caller() -> None:
    """SC-0's writer was reachable only from tests. This one may not be.

    Read off the source tree rather than asserted on a mock: a test that only
    patched the writer would keep passing after a refactor dropped the real
    call, which is exactly how the predecessor ended up orphaned.
    """
    callers = sorted(
        path.relative_to(_REPO_ROOT).as_posix()
        for path in (_REPO_ROOT / "kew" / "src" / "kew").rglob("*.py")
        if path.name != "audit.py"
        and "append_runtime_egress_certification" in path.read_text(encoding="utf-8")
    )
    assert callers, (
        "append_runtime_egress_certification has no caller under src/ — the "
        "admission gate would demand a proof nothing can write"
    )
    assert "kew/src/kew/openshell_egress.py" in callers


def _probe_policies(runner: str, flow: str = "dispatch") -> list[bytes]:
    """The per-binary probe policies the shipped ``flow`` policy splits into."""
    from kew.openshell_egress import (
        binary_probe_policy,
        governed_binary_clauses,
        openshell_policy_path,
    )

    header, clauses = governed_binary_clauses(
        openshell_policy_path(runner, flow=flow).read_bytes()
    )
    return [binary_probe_policy(header, body) for _, body in clauses]


def test_a_failing_boundary_records_nothing_and_still_deletes_the_sandbox(
    tmp_path: Path, certify_host: Any
) -> None:
    """A leak is reported to the operator, never left on the chain."""
    from kew.openshell_egress import certify_openshell_egress

    client = _FakeClient(_probe_output(github="reachable"))
    config = certify_host(client)
    with SqliteRunStore(str(tmp_path / "failed.db")) as store:
        result = certify_openshell_egress(config, runner="claude-code", store=store)
        assert not result.passed
        assert result.event_id is None
        assert store.list_audit_events(limit=None) == []
    assert client.deleted == [created["name"] for created in client.created]


def test_a_probe_that_cannot_be_read_still_deletes_the_sandbox(
    tmp_path: Path, certify_host: Any
) -> None:
    """The cleanup is in a ``finally``: a broken probe leaves no container."""
    from kew.openshell_egress import EgressProbeError, certify_openshell_egress

    client = _FakeClient("the gateway said something else entirely")
    config = certify_host(client)
    with (
        SqliteRunStore(str(tmp_path / "unreadable.db")) as store,
        pytest.raises(EgressProbeError, match="result lines"),
    ):
        certify_openshell_egress(config, runner="claude-code", store=store)
    assert client.deleted == [client.created[0]["name"]]


def test_a_surviving_probe_sandbox_is_reported_not_swallowed(
    tmp_path: Path, certify_host: Any
) -> None:
    """A delete that failed is an operator's problem, said out loud."""
    from kew.openshell import OpenShellError
    from kew.openshell_egress import certify_openshell_egress

    client = _FakeClient(
        _probe_output(),
        delete_error=OpenShellError(("openshell", "sandbox", "delete"), reason="busy"),
    )
    config = certify_host(client)
    with SqliteRunStore(str(tmp_path / "survivor.db")) as store:
        result = certify_openshell_egress(config, runner="claude-code", store=store)
        assert store.list_audit_events(limit=None) == []
    assert result.cleanup_error is not None
    assert "busy" in result.cleanup_error
    # The matrix held; the run still is not a pass and recorded nothing.
    assert result.certification.verdict == "passed"
    assert not result.passed
    assert result.event_id is None


def test_the_probe_runs_a_binary_every_policy_governs(
    tmp_path: Path, certify_host: Any
) -> None:
    """An ungoverned binary is denied everything, so it certifies nothing.

    Under ``enforcement: enforce`` a binary absent from the policy has no
    endpoint at all. A probe run through such a binary would record eight
    denials and a failed positive control — or, worse, eight denials beside a
    control that some future policy happened to allow. Node is governed in
    every kew policy with the runner's inference host, which is why the probe
    is a Node program.
    """
    from kew.openshell_egress import PROBE_INTERPRETER, certify_openshell_egress

    client = _FakeClient(_probe_output())
    config = certify_host(client)
    with SqliteRunStore(str(tmp_path / "governed.db")) as store:
        certify_openshell_egress(config, runner="claude-code", store=store)
    assert client.argv[0] == PROBE_INTERPRETER
    governed = {binary["path"] for binary in _policy("claude-code")["binaries"]}
    assert PROBE_INTERPRETER in governed


def _policy(runner: str, flow: str = "dispatch") -> dict[str, Any]:
    from kew.openshell_egress import policy_file_stem

    stem = policy_file_stem(runner, flow)
    document = yaml.safe_load(
        (_REPO_ROOT / "docker" / "openshell" / f"policy-{stem}.yaml").read_text(
            encoding="utf-8"
        )
    )
    assert isinstance(document, dict)
    return document


def _shipped_policy_boundaries() -> list[tuple[str, str]]:
    """Every (flow, runner) with a packaged OpenShell policy, read off the tree.

    Globbed rather than listed, so a policy added without an endpoint-host entry
    fails the coupling test below instead of shipping a boundary that cannot be
    certified. ``policy-codex.yaml`` shipped at #2371 and the map did not
    follow, which is exactly the gap this closes (#2389 review).

    One image now carries more than one boundary (epic node N12), so a file name
    names a flow as well as a runner: the longest flow prefix wins, which is why
    ``review-provision-claude-code`` is not read as the ``review`` flow of a
    runner called ``provision-claude-code``.
    """
    from kew.openshell_egress import DISPATCH_FLOW, OPENSHELL_POLICY_HOSTS

    flows = sorted(
        (flow for flow in OPENSHELL_POLICY_HOSTS if flow != DISPATCH_FLOW),
        key=len,
        reverse=True,
    )
    boundaries = []
    for path in (_REPO_ROOT / "docker" / "openshell").glob("policy-*.yaml"):
        stem = path.name[len("policy-") : -len(".yaml")]
        flow = next((name for name in flows if stem.startswith(f"{name}-")), None)
        if flow is None:
            boundaries.append((DISPATCH_FLOW, stem))
        else:
            boundaries.append((flow, stem[len(flow) + 1 :]))
    return sorted(boundaries)


@pytest.mark.policy
def test_every_shipped_policy_has_an_inference_host_entry() -> None:
    """A packaged policy with no map entry is a boundary kew cannot certify.

    `kew sandbox certify --runner X` refuses an unmapped runner with "has no
    OpenShell policy to certify", which for a runner whose policy IS packaged
    is a false statement about kew's own tree.
    """
    from kew.openshell_egress import DISPATCH_FLOW, OPENSHELL_POLICY_HOSTS

    shipped = _shipped_policy_boundaries()
    assert shipped, "no packaged OpenShell policies were found"
    assert (DISPATCH_FLOW, "codex") in shipped
    mapped = sorted(
        (flow, runner)
        for flow, runners in OPENSHELL_POLICY_HOSTS.items()
        for runner in runners
    )
    assert mapped == shipped


@pytest.mark.policy
@pytest.mark.parametrize("boundary", _shipped_policy_boundaries())
def test_the_positive_control_host_is_an_endpoint_the_policy_allows(
    boundary: tuple[str, str],
) -> None:
    """The control must dial a host the policy allows — and nothing else.

    Production code cannot read the policy (PyYAML is not a kew dependency), so
    the endpoint-host map is a constant. This is the coupling that keeps it
    true: widen a policy and this fails until the map follows. The map carries
    the policy's WHOLE endpoint set — ChatGPT-mode Codex needs three — and the
    first entry is the one the positive control dials, so it has to be among
    them.

    Every flow is checked, including the one no probe can certify: the map is
    what every OTHER reader of that boundary consults, and a provisioning policy
    whose hosts it misreported would be wrong everywhere a probe is not run.
    """
    from kew.openshell_egress import openshell_inference_host, policy_flow_hosts

    flow, runner = boundary
    hosts = {
        endpoint["host"]
        for binary in _policy(runner, flow)["binaries"]
        for endpoint in binary.get("endpoints", [])
    }
    assert hosts == set(policy_flow_hosts(flow)[runner])
    assert openshell_inference_host(runner, flow=flow) in hosts


def test_the_review_flow_is_certified_against_its_own_policy_digest(
    tmp_path: Path, certify_host: Any
) -> None:
    """``--flow review`` proves the reviewer's boundary, and only that one.

    Same image, same gateway, same launcher — a different policy file, so a
    different bound tuple. Without this the reviewer would be admitted on a
    dispatch proof, which says nothing about the launch that executes the
    reviewed PR's tests (epic node N12).
    """
    from kew.egress_certification import file_digest
    from kew.openshell_egress import (
        DISPATCH_FLOW,
        REVIEW_FLOW,
        certify_openshell_egress,
        openshell_policy_path,
    )

    client = _FakeClient(_probe_output())
    config = certify_host(client)
    with SqliteRunStore(str(tmp_path / "review.db")) as store:
        review = certify_openshell_egress(
            config, runner="claude-code", store=store, flow=REVIEW_FLOW
        )
        assert review.passed
        # The review proof is on the chain; the dispatch tuple is still unproven.
        assert _match(
            store,
            policy_sha256=file_digest(
                openshell_policy_path("claude-code", flow=REVIEW_FLOW)
            ),
            gateway_identity=review.certification.gateway_identity,
            supervisor_digest=review.certification.supervisor_digest,
        )
        with pytest.raises(Exception, match="certification"):
            _match(
                store,
                policy_sha256=file_digest(
                    openshell_policy_path("claude-code", flow=DISPATCH_FLOW)
                ),
                gateway_identity=review.certification.gateway_identity,
                supervisor_digest=review.certification.supervisor_digest,
            )
    assert [created["policy"] for created in client.created] == _probe_policies(
        "claude-code", REVIEW_FLOW
    )
    assert client.created[0]["labels"]["kew.flow"] == REVIEW_FLOW


def test_the_provisioning_flow_is_refused_rather_than_probed(
    tmp_path: Path, certify_host: Any
) -> None:
    """A check that cannot mean anything must not be run and reported.

    The provisioning policy governs ``uv`` alone — the whole point, since every
    build backend uv executes is the reviewed PR's code — and the probe is a Node
    program. Probing it would record a failed positive control for a policy
    working exactly as written, which reads like a leaking boundary.
    """
    from kew.openshell_egress import (
        PROVISION_FLOW,
        EgressProbeError,
        certify_openshell_egress,
    )

    client = _FakeClient(_probe_output())
    config = certify_host(client)
    with (
        SqliteRunStore(str(tmp_path / "provision.db")) as store,
        pytest.raises(EgressProbeError, match="cannot be certified"),
    ):
        certify_openshell_egress(
            config, runner="claude-code", store=store, flow=PROVISION_FLOW
        )
    assert client.created == [], "a sandbox was created for a probe that cannot pass"


def test_an_unknown_policy_flow_is_named_rather_than_read_as_empty(
    tmp_path: Path, certify_host: Any
) -> None:
    """A flow typo must not resolve to "this build ships no policy at all"."""
    from kew.openshell_egress import EgressProbeError, certify_openshell_egress

    client = _FakeClient(_probe_output())
    config = certify_host(client)
    with (
        SqliteRunStore(str(tmp_path / "typo.db")) as store,
        pytest.raises(EgressProbeError, match="no OpenShell policy flow named"),
    ):
        certify_openshell_egress(
            config, runner="claude-code", store=store, flow="reviewe"
        )
    assert client.created == []


def test_the_probe_targets_are_routable_and_outside_every_policy() -> None:
    """A denied target must be reachable from an open host, or it proves nothing.

    A reserved-but-unroutable address times out on a wide-open boundary and
    would be recorded as denied — certifying the opposite of the truth. The
    one deliberate exception is the unresolvable name, which probes the
    resolver rather than a route.
    """
    from kew.openshell_egress import OPENSHELL_INFERENCE_HOSTS, probe_parameters

    parameters = probe_parameters(
        inference_host="api.anthropic.com",
        gateway_port=8080,
        forge_host="ghe.example.test",
    )
    dialled = {
        str(parameters[key])
        for key in ("external_host", "public_ip", "ipv6_host", "github_host")
    }
    allowed = {host for hosts in OPENSHELL_INFERENCE_HOSTS.values() for host in hosts}
    assert dialled.isdisjoint(allowed)
    assert not any(
        str(parameters[key]).startswith(("192.0.2.", "198.51.100.", "203.0.113."))
        for key in ("public_ip",)
    )
    assert str(parameters["unresolvable_name"]).endswith(".invalid")
    assert parameters["metadata_ip"] == "169.254.169.254"


@pytest.mark.parametrize(
    ("endpoint", "expected"),
    [
        ("http://127.0.0.1:8080", 8080),
        ("http://gateway.internal:9443/", 9443),
        ("", 8080),
        ("http://127.0.0.1", 8080),
        ("http://127.0.0.1:0", 8080),
    ],
)
def test_the_gateway_probe_always_has_a_concrete_port(
    endpoint: str, expected: int
) -> None:
    """The host-gateway probe never silently skips for want of a port."""
    from kew.openshell_egress import gateway_probe_port

    assert gateway_probe_port(endpoint) == expected


# ---------------------------------------------------------------------------
# Admission
# ---------------------------------------------------------------------------


def _openshell_config() -> Any:
    from kew.config import SandboxConfig

    return SandboxConfig(
        enabled="auto",
        runtime="openshell",
        images={"claude-code": "kew-agent:docker-000000000000"},
    )


def test_admission_refuses_openshell_without_a_certification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate: no proof, no claimed boundary — under its own token.

    Not ``runtime_unavailable`` (the host is not missing a runtime) and not
    ``openshell-runtime-unavailable`` (kew is not missing the boundary): the
    boundary is there, unproven, and one shipped command fixes it.
    """
    import kew.sandbox as sandbox

    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-test-value")
    monkeypatch.setattr(
        sandbox,
        "inspect_image_id",
        lambda runtime, image: built_by_kew(image, _IMAGE_ID),
    )
    with (
        SqliteRunStore(str(tmp_path / "admit.db")) as store,
        pytest.raises(sandbox.SandboxRuntimeUnavailable) as raised,
    ):
        sandbox.resolve_effective_sandbox(
            _openshell_config(), runner="claude-code", store=store
        )
    assert raised.value.reason == sandbox.REFUSAL_OPENSHELL_UNCERTIFIED
    assert "kew sandbox certify" in str(raised.value)


def test_admission_without_a_ledger_refuses_rather_than_assuming(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ "No ledger to check" is not a pass — it is the same refusal."""
    import kew.sandbox as sandbox

    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-test-value")
    with pytest.raises(sandbox.SandboxRuntimeUnavailable) as raised:
        sandbox.resolve_effective_sandbox(
            _openshell_config(), runner="claude-code", store=None
        )
    assert raised.value.reason == sandbox.REFUSAL_OPENSHELL_UNCERTIFIED


def test_a_matching_certification_clears_the_egress_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, certify_host: Any
) -> None:
    """With the proof on the chain, admission reaches the NEXT gate.

    That next gate is the launch path (epic node N6): it admits the dispatch
    shape, and refuses a shape the gateway cannot launch — a flow that runs its
    agent as a container argv — under a different reason with a different fix.
    Reaching either is what shows the egress gate passed rather than being
    skipped.
    """
    import kew.openshell as openshell
    import kew.sandbox as sandbox
    from kew.openshell_egress import certify_openshell_egress

    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-test-value")
    client = _FakeClient(_probe_output())
    config = certify_host(client)
    # The gate reads the gateway version itself, through its own lazy import.
    monkeypatch.setattr(
        openshell,
        "OpenShellClient",
        type("Stub", (), {"from_settings": staticmethod(lambda _s: client)}),
    )
    with SqliteRunStore(str(tmp_path / "cleared.db")) as store:
        certify_openshell_egress(config, runner="claude-code", store=store)
        # The dispatch shape is no longer refused at all: N6 launches it.
        decision = sandbox.resolve_effective_sandbox(
            config.sandbox, runner="claude-code", store=store
        )
        with pytest.raises(sandbox.SandboxRuntimeUnavailable) as raised:
            sandbox.resolve_effective_sandbox(
                config.sandbox, runner="claude-code", store=store, gateway_launch=False
            )
    assert decision.posture != "refused"
    assert raised.value.reason == sandbox.REFUSAL_OPENSHELL_UNAVAILABLE


def test_a_flow_launch_reads_the_certification_off_the_ledger_it_was_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, certify_host: Any
) -> None:
    """``resolve_flow_containment`` holds the ledger, so it must read it.

    The ci-fix and plan-run flows hand their store in as the first argument. It
    was threaded into the attestation but not into the resolution, so every one
    of those launches refused on "no ledger is available" with a fresh matching
    proof on that exact store (#2389 review). Passing means reaching the NEXT
    gate — the launch path (N6), which a container-argv flow such as CI fix
    cannot use — under its own refusal reason.
    """
    import kew.openshell as openshell
    import kew.sandbox as sandbox
    from kew.openshell_egress import certify_openshell_egress

    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-test-value")
    client = _FakeClient(_probe_output())
    config = certify_host(client)
    monkeypatch.setattr(
        openshell,
        "OpenShellClient",
        type("Stub", (), {"from_settings": staticmethod(lambda _s: client)}),
    )
    with SqliteRunStore(str(tmp_path / "flow.db")) as store:
        certify_openshell_egress(config, runner="claude-code", store=store)
        with pytest.raises(sandbox.SandboxRuntimeUnavailable) as raised:
            sandbox.resolve_flow_containment(
                store,
                flow="CI fix",
                runner="claude-code",
                config=config.sandbox,
                gateway_launch=False,
                run_id="flow-run",
            )
    assert raised.value.reason == sandbox.REFUSAL_OPENSHELL_UNAVAILABLE


def test_the_probe_route_reads_the_certification_off_the_ledger_it_was_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, certify_host: Any
) -> None:
    """``resolve_probe_route`` takes the ledger, so it must resolve with it.

    ``kew doctor route-live-confirm`` is the one command whose whole job is to
    prove this exact leg completes a turn, and it spends real money doing it.
    It took the store for the proxy digest and resolved containment without it,
    so every OpenShell probe refused on "no ledger is available" with a fresh
    matching proof on that exact store (#2389 review). Passing means reaching
    the NEXT gate — the missing runtime backend (N6) — under its own reason.
    """
    import kew.openshell as openshell
    import kew.sandbox as sandbox
    from kew.openshell_egress import certify_openshell_egress
    from kew.readiness_probe import resolve_probe_route

    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-test-value")
    client = _FakeClient(_probe_output())
    config = certify_host(client)
    monkeypatch.setattr(
        openshell,
        "OpenShellClient",
        type("Stub", (), {"from_settings": staticmethod(lambda _s: client)}),
    )
    with SqliteRunStore(str(tmp_path / "probe.db")) as store:
        certify_openshell_egress(config, runner="claude-code", store=store)
        with pytest.raises(sandbox.SandboxRuntimeUnavailable) as raised:
            resolve_probe_route(config, complexity="complex", store=store)
    assert raised.value.reason == sandbox.REFUSAL_OPENSHELL_UNAVAILABLE


def test_the_probe_route_without_a_ledger_still_refuses_uncertified(
    monkeypatch: pytest.MonkeyPatch, certify_host: Any
) -> None:
    """Threading the store must not become a way to skip the gate."""
    import kew.sandbox as sandbox
    from kew.readiness_probe import resolve_probe_route

    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-test-value")
    config = certify_host(_FakeClient(_probe_output()))
    with pytest.raises(sandbox.SandboxRuntimeUnavailable) as raised:
        resolve_probe_route(config, complexity="complex")
    assert raised.value.reason == sandbox.REFUSAL_OPENSHELL_UNCERTIFIED


def test_a_flow_launch_without_a_ledger_still_refuses_uncertified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Threading the store must not become a way to skip the gate."""
    import kew.sandbox as sandbox

    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-test-value")
    with pytest.raises(sandbox.SandboxRuntimeUnavailable) as raised:
        sandbox.resolve_flow_containment(
            None,
            flow="CI fix",
            runner="claude-code",
            config=_openshell_config(),
            gateway_launch=False,
        )
    assert raised.value.reason == sandbox.REFUSAL_OPENSHELL_UNCERTIFIED


def _remediation_host(
    monkeypatch: pytest.MonkeyPatch, client: Any, config: Any
) -> None:
    """Make the remediation module's own prerequisites hold on this host.

    It does not delegate to ``resolve_effective_sandbox`` on the auto path, so
    it detects the runtime and resolves the image itself — both have to be
    patched where IT reads them, not where ``sandbox`` does.
    """
    import kew.openshell as openshell
    import kew.remediation_containment as remediation_containment

    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-test-value")
    monkeypatch.setattr(
        remediation_containment, "detect_runtime_cached", lambda _config: "/bin/openshl"
    )
    monkeypatch.setattr(
        openshell,
        "OpenShellClient",
        type("Stub", (), {"from_settings": staticmethod(lambda _s: client)}),
    )
    config.sandbox.enabled = "auto"


def test_remediation_auto_does_not_claim_an_uncertified_openshell_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, certify_host: Any
) -> None:
    """The auto sub-path resolves containment itself, so it must gate it itself.

    It returned ``sandboxed`` with the egress gate never consulted at all: the
    one branch of the remediation decision that does not delegate to
    ``resolve_effective_sandbox`` was also the branch that skipped the proof
    (#2389 review). Auto keeps its host fallback, so the gap is recorded as a
    reason rather than raised — but it is never a claimed boundary.
    """
    from kew.remediation_containment import resolve_remediation_containment

    client = _FakeClient(_probe_output())
    config = certify_host(client)
    _remediation_host(monkeypatch, client, config)
    with SqliteRunStore(str(tmp_path / "remediate.db")) as store:
        decision = resolve_remediation_containment(
            config, runner="claude-code", store=store
        )
    assert decision.posture == "uncontained_flow"
    assert not decision.contained
    assert "kew sandbox certify" in (decision.reason or "")


def test_remediation_reads_the_certification_off_the_ledger_it_was_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, certify_host: Any
) -> None:
    """A fresh matching proof clears the egress gate, on both sub-paths.

    Review remediation and conflict resolution resolved with no ledger at all,
    so the required-containment path refused every OpenShell run as "no ledger
    is available to read the openshell egress certification" — naming a remedy
    the operator had already run, onto the very store they ran it against — and
    the auto path never asked. With the store threaded, the proof counts.

    It no longer CONTAINS the run, though: since N6 these flows are known to
    have no launch path on the gateway (they run their agent as a container
    argv), so past the egress gate the auto path records that gap as a host
    fallback and the required path refuses for it. Either way no boundary is
    claimed, and neither answer is the egress gate's.
    """
    import kew.sandbox as sandbox
    from kew.openshell_egress import certify_openshell_egress
    from kew.remediation_containment import resolve_remediation_containment

    client = _FakeClient(_probe_output())
    config = certify_host(client)
    _remediation_host(monkeypatch, client, config)
    with SqliteRunStore(str(tmp_path / "remediate-proof.db")) as store:
        certify_openshell_egress(config, runner="claude-code", store=store)
        decision = resolve_remediation_containment(
            config, runner="claude-code", store=store
        )
        assert decision.posture == "uncontained_flow"
        assert not decision.contained
        assert "kew sandbox certify" not in (decision.reason or "")
        assert "no ledger is available" not in (decision.reason or "")
        assert "no container argv" in (decision.reason or "")

        # The required-containment path delegates, and reaches the NEXT gate
        # (the launch path, N6) instead of "no ledger is available".
        config.sandbox.enabled = True
        with pytest.raises(sandbox.SandboxRuntimeUnavailable) as raised:
            resolve_remediation_containment(config, runner="claude-code", store=store)
    assert raised.value.reason == sandbox.REFUSAL_OPENSHELL_UNAVAILABLE


def test_remediation_without_a_ledger_still_refuses_uncertified(
    monkeypatch: pytest.MonkeyPatch, certify_host: Any
) -> None:
    """Threading the store must not become a way to skip the gate."""
    import kew.sandbox as sandbox
    from kew.remediation_containment import resolve_remediation_containment

    client = _FakeClient(_probe_output())
    config = certify_host(client)
    _remediation_host(monkeypatch, client, config)
    decision = resolve_remediation_containment(config, runner="claude-code", store=None)
    assert decision.posture == "uncontained_flow"
    assert "no ledger is available" in (decision.reason or "")

    config.sandbox.enabled = True
    with pytest.raises(sandbox.SandboxRuntimeUnavailable) as raised:
        resolve_remediation_containment(config, runner="claude-code", store=None)
    assert raised.value.reason == sandbox.REFUSAL_OPENSHELL_UNCERTIFIED


def test_the_openshell_route_records_its_own_proven_value() -> None:
    """``openshell-certified`` / ``openshell-selftest``, and nothing weaker."""
    from kew import sandbox

    classify = sandbox.classify_egress_verification
    assert classify(route="openshell", certified=True) == "openshell-certified"
    assert classify(route="openshell") == "unavailable"
    assert sandbox.OPENSHELL_ROUTE_EGRESS_VERIFICATION == "openshell-certified"
    assert sandbox.OPENSHELL_EGRESS_VERIFICATION_BASIS == "openshell-selftest"
    # The direct route's frozen value is untouched by the new route.
    assert classify(route="direct", certified=True) == "operator-managed-unverified"


def test_an_openshell_posture_names_whether_it_has_its_proof(
    tmp_path: Path,
) -> None:
    """The basis says which happened — never the sidecar's "pending"."""
    from kew import sandbox

    assert sandbox._route_verification_basis(
        "openshell", posture=sandbox.POSTURE_SANDBOXED, certified=True
    ) == {"egress_verification_basis": "openshell-selftest"}
    assert sandbox._route_verification_basis(
        "openshell", posture=sandbox.POSTURE_SANDBOXED, certified=False
    ) == {"egress_verification_basis": "openshell-uncertified"}
    assert (
        sandbox._route_verification_basis(
            "openshell", posture="refused", certified=False
        )
        == {}
    )
    assert tmp_path.is_dir()


def test_a_sandboxed_openshell_flow_is_never_attested_as_the_direct_route(
    tmp_path: Path,
) -> None:
    """The flow writer derives the route from the RUNTIME, not just the transport.

    ``attest_flow_posture`` used to derive "sidecar or direct", so a review
    remediation or conflict resolution contained by OpenShell was recorded with
    the direct route's permanent ``operator-managed-unverified`` — the one
    value no evidence can ever upgrade, written for a boundary whose proof kew
    had not read. That verdict may never carry it (#2389 review).
    """
    from kew import sandbox

    with SqliteRunStore(str(tmp_path / "flow.db")) as store:
        posture = sandbox.attest_flow_posture(
            store,
            flow="review remediation",
            runner="claude-code",
            config=_openshell_config(),
            posture=sandbox.POSTURE_SANDBOXED,
            image="kew-agent:docker-000000000000",
        )
        [event] = store.list_audit_events(event="sandbox_resolved", limit=None)
    assert posture == sandbox.POSTURE_SANDBOXED
    assert event.payload["egress_verification"] == "openshell-certified"
    assert event.payload["egress_verification_basis"] == "openshell-selftest"
    assert event.payload["egress_verification"] != (
        sandbox.DIRECT_ROUTE_EGRESS_VERIFICATION
    )
    # The direct route's network evidence belongs to the direct route alone.
    assert "network_driver" not in event.payload


def test_a_contained_openshell_flow_records_the_proof_it_had_to_hold(
    tmp_path: Path,
) -> None:
    """A sandboxed OpenShell posture is certified BY CONSTRUCTION, and says so.

    The remediation and ci-fix flows attested every OpenShell container as
    ``openshell-uncertified`` — correct while they resolved without a ledger,
    and a falsehood the moment they started reading one, because the only way
    to a sandboxed OpenShell posture now runs through the egress gate (#2389
    review). The three cases that are NOT that posture keep their own answers.
    """
    from kew import sandbox
    from kew.config import SandboxConfig

    config = _openshell_config()
    certified = sandbox.openshell_boundary_certified
    assert certified(config, posture=sandbox.POSTURE_SANDBOXED, sidecar=False)
    # A sidecar's proof is the packet certification, not this one.
    assert not certified(config, posture=sandbox.POSTURE_SANDBOXED, sidecar=True)
    # Nothing was contained, so there is no boundary to have proven.
    assert not certified(config, posture="uncontained_flow", sidecar=False)
    # Another runtime has no certification contract at all.
    assert not certified(
        SandboxConfig(runtime="docker"),
        posture=sandbox.POSTURE_SANDBOXED,
        sidecar=False,
    )

    with SqliteRunStore(str(tmp_path / "uncontained.db")) as store:
        sandbox.attest_flow_posture(
            store,
            flow="conflict resolution",
            runner="claude-code",
            config=config,
            posture="uncontained_flow",
            reason="sandbox runtime not found",
        )
        [event] = store.list_audit_events(event="sandbox_resolved", limit=None)
    assert event.payload["egress_verification"] == "unavailable"
    assert "egress_verification_basis" not in event.payload


def test_one_derivation_decides_the_route_for_both_writers() -> None:
    """Dispatch and the flow writer must not disagree about the route.

    Two inline derivations is how the flow writer ended up naming the direct
    route for an OpenShell container while dispatch named ``openshell``.
    """
    from kew import sandbox
    from kew.dispatch_core import _contained_egress_route

    decision = sandbox.SandboxDecision(
        sandboxed=True,
        runtime="openshell",
        posture=sandbox.POSTURE_SANDBOXED,
        notice=None,
        image="kew-agent:docker-000000000000",
    )
    assert _contained_egress_route(None, decision) == "openshell"
    assert (
        sandbox.contained_egress_route(
            posture=sandbox.POSTURE_SANDBOXED, runtime="openshell", sidecar=False
        )
        == "openshell"
    )
    # A sidecar transport wins: the worker's packets leave through the proxy.
    assert (
        sandbox.contained_egress_route(
            posture=sandbox.POSTURE_SANDBOXED, runtime="openshell", sidecar=True
        )
        == "sidecar"
    )
    # No container, no contained route to name.
    assert (
        sandbox.contained_egress_route(
            posture="uncontained_flow", runtime="openshell", sidecar=False
        )
        == "direct"
    )
    assert (
        sandbox.contained_egress_route(
            posture=sandbox.POSTURE_SANDBOXED, runtime="docker", sidecar=False
        )
        == "direct"
    )


#: Every helper that can resolve an OpenShell boundary, and therefore has to be
#: handed the ledger :func:`require_openshell_egress_certification` reads the
#: proof from. Listed by name because that is what a call site shows.
_LEDGER_READING_RESOLVERS = frozenset(
    {
        "_resolve_dispatch_sandbox",
        "_resolve_effective_sandbox",
        "require_certified_boundary",
        "resolve_effective_sandbox",
        "resolve_effective_sidecar_sandbox",
        "resolve_transport_sandbox",
    }
)

#: Every module that resolves an OpenShell boundary, launch path or not: an
#: unthreaded ledger turns into a refused dispatch on the launch paths and into
#: a false diagnostic everywhere else, and both say "no ledger is available"
#: about a ledger that holds the proof.
#:
#: ``remediation_containment.py`` is the third: review remediation and conflict
#: resolution do not go through ``sandbox.resolve_flow_containment`` at all,
#: they have their own decision module, and it was resolving without the ledger
#: on every branch (#2389 review).
#:
#: ``readiness_probe.py`` is the fourth. It reads as a diagnostic and is not
#: one: ``kew doctor route-live-confirm`` launches across the boundary this
#: resolves and spends real money doing it, so a storeless resolution refused
#: every OpenShell probe as uncertified — the only command whose whole job is
#: to prove that exact leg works (#2389 review).
#:
#: ``cli/doctor.py`` is the fifth, and the one that was left off this list on
#: the theory that a surface which only DESCRIBES a posture need not read the
#: proof. It describes it BY resolving it, so bare ``kew doctor`` reported
#: ``containment: FAIL (openshell-egress-uncertified)`` and prescribed a
#: ``kew sandbox certify`` that had already run (#2389 review).
#: ``graph/driver/review_launcher.py`` is the sixth, and it was not resolving a
#: boundary at all before epic node N12: the most exposed launch kew makes went
#: through no gate and attested no posture. It is on this list from the start so
#: its gate can never be the one that drops the ledger.
_BOUNDARY_RESOLVING_MODULES = (
    "cli/doctor.py",
    "dispatch_core.py",
    "graph/driver/review_launcher.py",
    # The probe's route resolution moved out of readiness_probe.py (#2302).
    "probe_route.py",
    "remediation_containment.py",
    "sandbox.py",
)


@pytest.mark.policy
@pytest.mark.parametrize("module", _BOUNDARY_RESOLVING_MODULES)
def test_every_sandbox_resolution_threads_the_ledger_it_reads(module: str) -> None:
    """A call site without the store resolves OpenShell as uncertified.

    Two call sites have now been the one the gate was not threaded into: the
    governor swap in ``dispatch_core`` and ``resolve_flow_containment``, which
    holds the ledger as its first parameter and still resolved without it — so
    every ci-fix and plan-run flow refused on "no ledger to check" with a
    matching proof on that exact store (#2389 review). The remediation decision
    module was the third, on every one of its branches, the readiness probe the
    fourth, and ``kew doctor`` the fifth. Read off the syntax tree, across every
    module that resolves a boundary rather than the one that broke, so a new
    call site cannot quietly omit it.
    """
    import ast

    source = (_REPO_ROOT / "kew" / "src" / "kew" / module).read_text(encoding="utf-8")
    calls = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in _LEDGER_READING_RESOLVERS
    ]
    assert calls, f"no ledger-reading resolution call sites were found in {module}"
    for call in calls:
        threaded = len(call.args) >= 4 or any(
            keyword.arg == "store" for keyword in call.keywords
        )
        assert threaded, (
            f"{call.func.id} at {module}:{call.lineno} omits the store, "
            "so an OpenShell boundary resolves as uncertified"
        )


def test_bare_doctor_reads_the_certification_off_the_project_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, certify_host: Any
) -> None:
    """`kew doctor` must not prescribe a certify that has already run.

    It resolved the boundary storeless, so a project whose own ledger held a
    fresh passing proof for that exact boundary was still told
    ``containment: FAIL (openshell-egress-uncertified)``, hinted that no ledger
    was available to read, and sent to ``kew sandbox certify`` — the command
    the operator had just run (#2389 review). Passing means getting past
    both OpenShell gates: since N6 the launch path admits this dispatch, so the
    report moves on to the runtime probe rather than stopping at either gate.
    """
    from typer.testing import CliRunner

    import kew.cli as cli
    import kew.cli.doctor as doctor
    import kew.openshell as openshell
    import kew.sandbox as sandbox
    from kew.openshell_egress import certify_openshell_egress

    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-test-value")
    client = _FakeClient(_probe_output())
    config = certify_host(client)
    config.logging.dir = str(tmp_path / "logs")
    Path(config.logging.dir).mkdir(parents=True)
    monkeypatch.setattr(
        openshell,
        "OpenShellClient",
        type("Stub", (), {"from_settings": staticmethod(lambda _s: client)}),
    )
    monkeypatch.setattr(doctor, "load_config", lambda: config)
    monkeypatch.setattr(doctor, "preflight_tools", lambda: None)
    monkeypatch.chdir(tmp_path)
    with SqliteRunStore(str(Path(config.logging.dir) / "dispatch.db")) as store:
        proof = certify_openshell_egress(config, runner="claude-code", store=store)
    assert proof.passed

    result = CliRunner().invoke(cli.app, ["doctor"])

    assert sandbox.REFUSAL_OPENSHELL_UNCERTIFIED not in result.output, result.output
    assert "no ledger is available" not in result.output
    assert "kew sandbox certify" not in result.output
    # Past both OpenShell gates, to the runtime probe. Whether that probe then
    # fails containment or takes the suite's admitted host path depends on
    # which `resolve_effective_sandbox` binding `kew.cli.doctor` holds, so
    # only the gates in front of it are pinned.
    assert (
        f"containment: FAIL ({sandbox.REFUSAL_OPENSHELL_UNAVAILABLE})"
        not in result.output
    )
    assert "runtime 'openshell' not found" in result.output


def test_bare_doctor_without_a_ledger_still_reports_uncertified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, certify_host: Any
) -> None:
    """Threading the ledger must not become a way to skip the gate.

    No ``dispatch.db`` at all: the diagnostic still fails closed and still
    names the one refusal a shipped command clears.
    """
    from typer.testing import CliRunner

    import kew.cli as cli
    import kew.cli.doctor as doctor
    import kew.openshell as openshell
    import kew.sandbox as sandbox

    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-test-value")
    client = _FakeClient(_probe_output())
    config = certify_host(client)
    config.logging.dir = str(tmp_path / "logs")
    Path(config.logging.dir).mkdir(parents=True)
    monkeypatch.setattr(
        openshell,
        "OpenShellClient",
        type("Stub", (), {"from_settings": staticmethod(lambda _s: client)}),
    )
    monkeypatch.setattr(doctor, "load_config", lambda: config)
    monkeypatch.setattr(doctor, "preflight_tools", lambda: None)
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(cli.app, ["doctor"])

    assert (
        f"containment: FAIL ({sandbox.REFUSAL_OPENSHELL_UNCERTIFIED})" in result.output
    ), result.output
    assert "kew sandbox certify" in result.output


def test_the_shipped_command_records_the_proof_it_demands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, certify_host: Any
) -> None:
    """`kew sandbox certify` end to end: exit 0, and the event on the chain."""
    from typer.testing import CliRunner

    import kew.cli as cli
    from kew.audit import RUNTIME_EGRESS_CERTIFICATION_EVENT

    client = _FakeClient(_probe_output())
    config = certify_host(client)
    config.logging.dir = str(tmp_path / "logs")
    Path(config.logging.dir).mkdir(parents=True)
    monkeypatch.setattr(cli, "load_config", lambda: config)
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(
        cli.app, ["sandbox", "certify", "--runtime", "openshell"]
    )
    assert result.exit_code == 0, result.output
    assert "egress proven closed" in result.output
    with SqliteRunStore(str(Path(config.logging.dir) / "dispatch.db")) as store:
        [event] = store.list_audit_events(
            event=RUNTIME_EGRESS_CERTIFICATION_EVENT, limit=None
        )
    assert event.payload["packet_verdict"] == "passed"


def test_a_proof_that_cannot_be_recorded_is_not_reported_as_a_leak(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, certify_host: Any
) -> None:
    """A ledger that refuses the append is a storage fault, not a boundary one.

    The probe PASSED and only the audit append failed, and the command did not
    catch it: it exited 1 with an uncaught traceback, and exit 1 for
    ``sandbox`` is documented as "Egress certification failed" — so an
    unwritable ledger was reported as a boundary that leaked (#2389 review).
    """
    from typer.testing import CliRunner

    import kew.audit as audit
    import kew.cli as cli
    from kew.audit import (
        RUNTIME_EGRESS_CERTIFICATION_EVENT,
        SidecarEvidencePersistenceError,
    )

    client = _FakeClient(_probe_output())
    config = certify_host(client)
    config.logging.dir = str(tmp_path / "logs")
    Path(config.logging.dir).mkdir(parents=True)
    monkeypatch.setattr(cli, "load_config", lambda: config)
    monkeypatch.chdir(tmp_path)

    def _refuse_append(*_args: Any, **_kwargs: Any) -> int:
        raise SidecarEvidencePersistenceError(
            "runtime egress certification could not be persisted"
        )

    monkeypatch.setattr(audit, "append_runtime_egress_certification", _refuse_append)

    result = CliRunner().invoke(
        cli.app, ["sandbox", "certify", "--runtime", "openshell"]
    )

    assert result.exit_code == 2, result.output
    assert not isinstance(result.exception, SidecarEvidencePersistenceError)
    assert "could not record it" in result.output
    # Never the exit-1 vocabulary: the boundary held.
    assert "FAILED" not in result.output
    assert "egress verdict" not in result.output
    with SqliteRunStore(str(Path(config.logging.dir) / "dispatch.db")) as store:
        assert (
            store.list_audit_events(
                event=RUNTIME_EGRESS_CERTIFICATION_EVENT, limit=None
            )
            == []
        )


def test_the_shipped_command_refuses_a_runtime_with_no_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Docker has no certification contract, and the refusal says so."""
    from typer.testing import CliRunner

    import kew.cli as cli
    from kew.config import DispatchConfig

    config = DispatchConfig()
    config.logging.dir = str(tmp_path / "logs")
    monkeypatch.setattr(cli, "load_config", lambda: config)

    result = CliRunner().invoke(cli.app, ["sandbox", "certify", "--runtime", "docker"])
    assert result.exit_code == 2
    assert "no egress contract" in result.output


def test_a_failing_boundary_exits_nonzero_without_recording(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, certify_host: Any
) -> None:
    """Exit 1 names the verdict and states that nothing was recorded."""
    from typer.testing import CliRunner

    import kew.cli as cli

    client = _FakeClient(_probe_output(host_gateway="reachable"))
    config = certify_host(client)
    config.logging.dir = str(tmp_path / "logs")
    Path(config.logging.dir).mkdir(parents=True)
    monkeypatch.setattr(cli, "load_config", lambda: config)
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(
        cli.app, ["sandbox", "certify", "--runtime", "openshell"]
    )
    assert result.exit_code == 1
    assert "nothing was recorded" in result.output
    from kew.audit import RUNTIME_EGRESS_CERTIFICATION_EVENT

    with SqliteRunStore(str(Path(config.logging.dir) / "dispatch.db")) as store:
        assert (
            store.list_audit_events(
                event=RUNTIME_EGRESS_CERTIFICATION_EVENT, limit=None
            )
            == []
        )


def test_an_already_reaped_probe_sandbox_is_not_a_cleanup_failure(
    tmp_path: Path, certify_host: Any
) -> None:
    """ "Not found" is the goal: nothing survives, so nothing is reported."""
    from kew.openshell import OpenShellNotFound
    from kew.openshell_egress import certify_openshell_egress

    client = _FakeClient(
        _probe_output(),
        delete_error=OpenShellNotFound(
            ("openshell", "sandbox", "delete"), reason="not found"
        ),
    )
    config = certify_host(client)
    with SqliteRunStore(str(tmp_path / "reaped.db")) as store:
        result = certify_openshell_egress(config, runner="claude-code", store=store)
    assert result.passed
    assert result.cleanup_error is None


# ---------------------------------------------------------------------------
# What the certification binds (#2624)
# ---------------------------------------------------------------------------


def _interpreter_clause(policy: bytes) -> str:
    """The clause OpenShell would apply to the probe: the one keyed on node.

    The fake gateway enforces per binary the way the real one does, so a probe
    run as node observes node's clause and nothing else.
    """
    from kew.openshell_egress import PROBE_INTERPRETER, governed_binary_clauses

    _, clauses = governed_binary_clauses(policy)
    return dict(clauses)[PROBE_INTERPRETER]


def _per_clause_probe(policy: bytes) -> str:
    """A probe whose ``external_host`` leaks exactly when its clause allows it."""
    if "example.com" in _interpreter_clause(policy):
        return _probe_output(external_host="reachable")
    return _probe_output()


def _widened_policy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The shipped claude-code policy with ONLY the native binary's clause widened."""
    import kew.openshell_egress as openshell_egress

    native = "/usr/lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe"
    shipped = openshell_egress.openshell_policy_path("claude-code").read_text(
        encoding="utf-8"
    )
    clause = f"  - path: {native}\n    endpoints:\n"
    assert shipped.count(clause) == 1
    widened = shipped.replace(
        clause,
        clause + "      - host: example.com\n        ports: [443]\n"
        "        enforcement: enforce\n",
    )
    path = tmp_path / "policy-claude-code.yaml"
    path.write_text(widened, encoding="utf-8")
    monkeypatch.setattr(
        openshell_egress, "openshell_policy_path", lambda runner, *, flow: path
    )
    return path


def test_a_widened_runner_clause_alone_fails_the_certification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, certify_host: Any
) -> None:
    """The probe runs as node; the runner's native binary has its own clause.

    Widen only that clause and a probe run under the whole policy still sees
    node's untouched rules and certifies. Each governed binary's clause is
    probed on its own, so the widened one is the matrix that fails.
    """
    from kew.openshell_egress import certify_openshell_egress

    _widened_policy(tmp_path, monkeypatch)
    client = _FakeClient(_per_clause_probe)
    config = certify_host(client)
    with SqliteRunStore(str(tmp_path / "widened.db")) as store:
        result = certify_openshell_egress(config, runner="claude-code", store=store)
        assert store.list_audit_events(limit=None) == []
    verdicts = {
        binary: packet.verdict for binary, packet in result.certification.packets
    }
    assert verdicts == {
        "/usr/bin/claude": "passed",
        "/usr/lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe": "failed",
        "/usr/bin/node": "passed",
    }
    assert result.certification.verdict == "failed"
    assert not result.passed
    assert result.event_id is None
    assert len(client.deleted) == 3


@pytest.mark.parametrize(
    ("runner", "flow"),
    [
        ("claude-code", "dispatch"),
        ("codex", "dispatch"),
        ("opencode", "dispatch"),
        ("claude-code", "review"),
    ],
)
def test_each_probe_policy_is_one_shipped_clause_granted_to_the_probe(
    runner: str, flow: str
) -> None:
    """The split is checked against a real YAML parser, clause by clause.

    Production code does not parse YAML, so this is what holds its line split
    to what OpenShell will read: every derived policy carries the shipped
    top-level fields and exactly one binary — the probe interpreter, with the
    endpoints of the clause it stands in for.
    """
    from kew.openshell_egress import (
        PROBE_INTERPRETER,
        binary_probe_policy,
        governed_binary_clauses,
        openshell_policy_path,
    )

    shipped = _policy(runner, flow)
    header, clauses = governed_binary_clauses(
        openshell_policy_path(runner, flow=flow).read_bytes()
    )
    assert [path for path, _ in clauses] == [
        binary["path"] for binary in shipped["binaries"]
    ]
    for (_, body), binary in zip(clauses, shipped["binaries"], strict=True):
        derived = yaml.safe_load(binary_probe_policy(header, body))
        assert derived["binaries"] == [{**binary, "path": PROBE_INTERPRETER}]
        assert {k: v for k, v in derived.items() if k != "binaries"} == {
            k: v for k, v in shipped.items() if k != "binaries"
        }


@pytest.mark.parametrize(
    "tail",
    [
        "network: open\n",
        '  - path: "/usr/bin/curl"\n',
        "  - path: /usr/bin/node\n",
        "  -   path: /usr/bin/curl\n",
    ],
)
def test_a_policy_the_split_cannot_read_is_refused(tail: str) -> None:
    """A clause the split misread would be a clause nobody probed."""
    from kew.openshell_egress import EgressProbeError, governed_binary_clauses

    policy = (
        b"version: '0.1'\nbinaries:\n  - path: /usr/bin/node\n    endpoints: []\n"
        + tail.encode()
    )
    with pytest.raises(EgressProbeError):
        governed_binary_clauses(policy)


def test_the_proof_names_the_gateway_it_was_taken_on(
    tmp_path: Path, certify_host: Any
) -> None:
    """Certify on gateway A, then launch on gateway B: B has no proof.

    The CLI version on this host is the same for both, which is all the old
    binding held. The identity comes from what each gateway says of itself.
    """
    from kew.audit import EgressCertificationUnavailable
    from kew.openshell_egress import certify_openshell_egress
    from kew.sandbox import require_openshell_boundary_match

    certified = _FakeClient(_probe_output())
    config = certify_host(certified)
    with SqliteRunStore(str(tmp_path / "gateway.db")) as store:
        result = certify_openshell_egress(config, runner="claude-code", store=store)
        [event] = store.list_audit_events(limit=None)
        assert event.payload["gateway_identity"] == (
            result.certification.gateway_identity
        )
        bound = {
            "image_id": _IMAGE_ID,
            "policy_sha256": result.certification.policy_sha256,
        }
        require_openshell_boundary_match(store, client=certified, **bound)
        for other in (
            _FakeClient("", endpoint="https://10.0.0.9:8443"),
            _FakeClient("", sandbox_image="openshell-sandbox@sha256:" + "9" * 64),
        ):
            assert other.check_version() == certified.check_version()
            with pytest.raises(EgressCertificationUnavailable):
                require_openshell_boundary_match(store, client=other, **bound)


def test_the_policy_is_read_once_and_the_probe_runs_those_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, certify_host: Any
) -> None:
    """A swap after the read is neither probed nor recorded.

    The file is replaced by a widened one between the read and the first
    create (inside the gateway version check, which runs in that window). The
    digest on the chain and the bytes every probe sandbox got are the read.
    """
    import kew.openshell_egress as openshell_egress
    from kew.egress_certification import bytes_digest

    shipped = openshell_egress.openshell_policy_path("claude-code").read_bytes()
    expected = _probe_policies("claude-code")
    path = _widened_policy(tmp_path, monkeypatch)
    swapped = path.read_bytes()
    path.write_bytes(shipped)

    class _SwappingClient(_FakeClient):
        def check_version(self) -> str:
            path.write_bytes(swapped)
            return super().check_version()

    client = _SwappingClient(_per_clause_probe)
    config = certify_host(client)
    with SqliteRunStore(str(tmp_path / "swap.db")) as store:
        result = openshell_egress.certify_openshell_egress(
            config, runner="claude-code", store=store
        )
    assert path.read_bytes() == swapped
    assert result.passed
    assert result.certification.policy_sha256 == bytes_digest(shipped)
    assert [created["policy"] for created in client.created] == expected


def test_the_launcher_bound_is_the_one_read_inside_the_image(
    tmp_path: Path, certify_host: Any
) -> None:
    """An image carrying another launcher is refused, not certified.

    The host's package copy is what the old binding hashed; the image that
    runs could carry anything. The probe reads the image's own copy.
    """
    from kew.openshell_egress import (
        LAUNCHER_PATH,
        EgressProbeError,
        certify_openshell_egress,
        probe_parameters,
    )

    assert (
        probe_parameters(
            inference_host="api.anthropic.com",
            gateway_port=8080,
            forge_host="github.com",
        )["launcher_path"]
        == LAUNCHER_PATH
    )
    for launcher, refusal in (("0" * 64, "rebuild the image"), ("", "could not read")):
        client = _FakeClient(_probe_output(launcher=launcher))
        config = certify_host(client)
        with (
            SqliteRunStore(str(tmp_path / f"launcher{len(launcher)}.db")) as store,
            pytest.raises(EgressProbeError, match=refusal),
        ):
            certify_openshell_egress(config, runner="claude-code", store=store)
        with SqliteRunStore(str(tmp_path / f"launcher{len(launcher)}.db")) as store:
            assert store.list_audit_events(limit=None) == []
        assert client.deleted == [created["name"] for created in client.created]


def test_a_probe_sandbox_that_survived_is_a_failed_certification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, certify_host: Any
) -> None:
    """A clean matrix beside an undeleted sandbox records nothing and exits 2."""
    from typer.testing import CliRunner

    import kew.cli as cli
    from kew.openshell import OpenShellError

    client = _FakeClient(
        _probe_output(),
        delete_error=OpenShellError(("openshell", "sandbox", "delete"), reason="busy"),
    )
    config = certify_host(client)
    config.logging.dir = str(tmp_path / "logs")
    Path(config.logging.dir).mkdir(parents=True)
    monkeypatch.setattr(cli, "load_config", lambda: config)
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(
        cli.app, ["sandbox", "certify", "--runtime", "openshell"]
    )
    assert result.exit_code == 2
    assert "nothing was recorded" in result.output
    assert "busy" in result.output
    from kew.audit import RUNTIME_EGRESS_CERTIFICATION_EVENT

    with SqliteRunStore(str(Path(config.logging.dir) / "dispatch.db")) as store:
        assert (
            store.list_audit_events(
                event=RUNTIME_EGRESS_CERTIFICATION_EVENT, limit=None
            )
            == []
        )
