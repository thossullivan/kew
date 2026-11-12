"""Runtime-neutral proof that a container boundary's egress is closed.

``kew.readiness`` already had the right *shape* for this (SC-0's
:class:`~kew.readiness.SidecarPacketCertification`), bound to the sidecar's two
immutable images and its generated packet filter. A third containment runtime
has neither: OpenShell's boundary is one image, one policy file, the gateway
that enforced it and the in-sandbox program that launched the agent under it.
So the certification here is bound to that runtime-neutral tuple —
``(image_id, policy_sha256, gateway_version, gateway_identity,
supervisor_digest)`` — and carries the runtime it was observed on, rather than
naming a sidecar's parts.

**One matrix per governed binary.** An OpenShell policy grants egress per
binary path, and the runner's own native binary is not the interpreter a probe
can run. So the certification carries one complete matrix for EVERY binary the
policy governs, each observed under that binary's own clause, and passes only
when all of them did: a policy whose runner clause alone was widened must not
certify on the strength of its interpreter clause (#2624).

Three properties make this a proof rather than a label, and each is enforced
here instead of at the call site:

**A positive control, from inside the boundary — in the leg's own protocol.** A
sandbox with no working network at all denies every packet, so a denial matrix
on its own certifies nothing — it is the single most plausible way this gate
silently passes while proving the opposite of what it claims. Most denial
outcomes therefore carry the same positive control (a TLS connection, from
inside the sandbox, to the runner's one allowed inference host), and
:func:`evaluate_topology_denial` fails the outcome when that control did not
succeed. That control is TCP over IPv4, which says nothing about a sandbox with
no UDP path or no IPv6 route, so the two legs that are neither
(:data:`OPENSHELL_PACKET_CONTROLS`) carry their own same-protocol control and
fail when it did not hold. kew would rather refuse a boundary whose IPv6 is
merely absent than certify one whose IPv6 it never tested.

**The matrix is complete or the certification is invalid.** The required
denial names per runtime are a module constant; a certification whose matrix
is short, reordered or renamed raises rather than passing on what it did
observe. A boundary is not partially closed.

**A probe that could not be run is not a denial.** Every status comes from
:class:`~kew.readiness.PacketProbeStatus`, whose ``UNAVAILABLE`` member makes
"kew could not tell" a distinct, certification-failing third answer. Nothing in
this module turns an unknown into a clean default.

Writing one of these to the audit chain, and reading one back under the
freshness and tuple rules admission enforces, lives in :mod:`kew.audit`. The
live probe that produces one lives in :mod:`kew.openshell_egress`.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from kew.readiness import (
    PacketProbeStatus,
    TopologyProbeResult,
    evaluate_topology_denial,
)
from kew.runtime_names import OPENSHELL_RUNTIME

#: The complete OpenShell negative matrix, in canonical order. Each name is one
#: destination class a contained agent must not reach, and the set is chosen so
#: that no single enforcement mistake leaves every entry denied by accident:
#:
#: ``external_host``        a name outside the policy, resolved and dialled;
#: ``public_ip``            a routable address dialled with no DNS at all, so a
#:                          boundary that only filters names is caught;
#: ``udp_dns``              UDP to a public resolver — a different protocol and
#:                          a classic exfiltration channel that TCP rules miss;
#: ``ipv6_external``        the same destination over IPv6, because an IPv4-only
#:                          ruleset reads as closed and is not;
#: ``link_local_metadata``  169.254.169.254, the cloud credential endpoint;
#: ``host_gateway``         the sandbox's own default gateway — the host, and
#:                          with it every service bound on a loopback port;
#: ``github``              the project's own Git forge, deliberately absent
#:                          from every kew policy: the container holds no forge
#:                          token and the host broker owns every push. Read from
#:                          the project's origin remote, so an Enterprise install
#:                          probes ITS forge rather than a host it never uses;
#: ``unresolvable_name``   a reserved name that must not resolve to any real
#:                          address. A resolver that answers it would make a
#:                          name-keyed allowlist meaningless, so this one is a
#:                          probe of the resolver rather than of a route.
OPENSHELL_PACKET_DENIALS: Final[tuple[str, ...]] = (
    "external_host",
    "public_ip",
    "udp_dns",
    "ipv6_external",
    "link_local_metadata",
    "host_gateway",
    "github",
    "unresolvable_name",
)

#: Required denial matrix per runtime. A runtime absent from this map has no
#: certification contract, so one cannot be built for it.
RUNTIME_PACKET_DENIALS: Final[Mapping[str, tuple[str, ...]]] = {
    OPENSHELL_RUNTIME: OPENSHELL_PACKET_DENIALS,
}

#: The denied targets the shared positive control cannot speak for, because it
#: does not use their protocol. The shared control is one TLS connection over
#: TCP and IPv4; ``udp_dns`` is UDP and ``ipv6_external`` is IPv6, and a
#: sandbox with no UDP path or no IPv6 route reports both as ``denied``
#: whatever the policy enforces — the same false pass the shared control exists
#: to stop, one protocol down. Each therefore carries its OWN same-protocol
#: control in :attr:`TopologyProbeResult.positive_control`, and the leg fails
#: when that control did not hold rather than passing on an absent protocol.
OPENSHELL_PACKET_CONTROLS: Final[frozenset[str]] = frozenset(
    {"udp_dns", "ipv6_external"}
)

#: Which denials carry their own control, per runtime. Pinned here rather than
#: read from a payload: a stored certification that could widen this set would
#: let a forged leg claim a control nothing ran.
RUNTIME_PACKET_CONTROLS: Final[Mapping[str, frozenset[str]]] = {
    OPENSHELL_RUNTIME: OPENSHELL_PACKET_CONTROLS,
}

#: Schema version of the payload this module writes to the audit chain. ``2``
#: bound the gateway's own identity and replaced the one matrix with one per
#: governed binary (#2624). A version-1 proof never matches a version-2
#: binding, so every boundary has to be certified again — which is the point:
#: a version-1 proof does not say which gateway, or which binaries, it covered.
CERTIFICATION_SCHEMA_VERSION: Final = 2

_IMAGE_ID_RE: Final = re.compile(r"^sha256:[0-9a-f]{64}$")
_DIGEST_RE: Final = re.compile(r"^[0-9a-f]{64}$")
#: Deliberately narrow: the version string reaches an audit payload and an
#: operator's terminal, and it comes from a third-party CLI's stdout.
_VERSION_RE: Final = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}$")

#: An absolute image path with nothing a policy file or a terminal could read
#: as something else.
BINARY_PATH_RE: Final = re.compile(r"^/[0-9A-Za-z._@+/-]{1,4095}$")

_PACKET_FIELDS: Final = frozenset({"positive_control", "denials"})
_BINARY_PACKET_FIELDS: Final = frozenset({"binary", "packet"})
_CERTIFICATION_FIELDS: Final = frozenset(
    {
        "schema_version",
        "runtime",
        "image_id",
        "policy_sha256",
        "gateway_version",
        "gateway_identity",
        "supervisor_digest",
        "packets",
        "packet_verdict",
    }
)


def _require_consistent_outcome(
    outcome: TopologyProbeResult,
    positive_control: PacketProbeStatus,
    protocol_controls: frozenset[str],
) -> None:
    """Refuse an outcome whose verdict or control is not the one it records.

    A leg named in ``protocol_controls`` is the one kind that may carry a
    control of its own — its protocol differs from the shared control's, so the
    shared control proves nothing about it. Every other leg must carry the
    shared value exactly, which is what stops a matrix whose denials each claim
    a control that was never run.
    """
    if (
        outcome.name not in protocol_controls
        and outcome.positive_control is not positive_control
    ):
        raise ValueError("egress packet outcome disagrees with the positive control")
    expected = evaluate_topology_denial(
        name=outcome.name,
        positive_control=outcome.positive_control,
        worker=outcome.worker,
    )
    if outcome != expected:
        raise ValueError("egress packet outcome verdict is inconsistent")


@dataclass(frozen=True)
class EgressPacketEvidence:
    """One positive control and the denial matrix observed beside it.

    ``positive_control`` is the *shared* control: every outcome in ``denials``
    must carry the same value, because they were all observed through the one
    connection that proved the sandbox has working egress to its allowed host.
    Storing it once and requiring agreement is what stops a later caller
    assembling a matrix whose denials each claim a control that was never run.

    ``protocol_controls`` names the exceptions — the legs whose protocol the
    shared control does not speak (see :data:`OPENSHELL_PACKET_CONTROLS`),
    which carry their own same-protocol control instead. It is NOT read from a
    stored payload: :meth:`RuntimeEgressCertification._require_complete_matrix`
    pins it to the runtime's own contract, so a forged certification cannot
    widen the set of legs allowed to supply their own control.
    """

    positive_control: PacketProbeStatus
    denials: tuple[TopologyProbeResult, ...]
    protocol_controls: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not isinstance(self.positive_control, PacketProbeStatus):
            raise ValueError("egress positive control must be a PacketProbeStatus")
        denials = tuple(self.denials)
        if not denials:
            raise ValueError("egress packet evidence requires a denial matrix")
        names = tuple(outcome.name for outcome in denials)
        if len(set(names)) != len(names):
            raise ValueError("egress packet evidence repeats a denied target")
        controls = frozenset(self.protocol_controls)
        if not controls <= set(names):
            raise ValueError("egress packet evidence controls an untested target")
        for outcome in denials:
            _require_consistent_outcome(outcome, self.positive_control, controls)
        object.__setattr__(self, "denials", denials)
        object.__setattr__(self, "protocol_controls", controls)

    @property
    def names(self) -> tuple[str, ...]:
        """The denied targets observed, in the order they were recorded."""
        return tuple(outcome.name for outcome in self.denials)

    @property
    def verdict(self) -> str:
        """``passed``, ``failed`` or ``unavailable`` — never a clean default.

        Every leg's own control counts, not just the shared one: a leg whose
        same-protocol control could not be attempted is a leg kew cannot read,
        and ``unavailable`` is the answer for that — not ``failed``, which
        would claim the boundary was observed to leak.
        """
        statuses = (
            self.positive_control,
            *(outcome.positive_control for outcome in self.denials),
            *(outcome.worker for outcome in self.denials),
        )
        if PacketProbeStatus.UNAVAILABLE in statuses:
            return "unavailable"
        if self.positive_control is PacketProbeStatus.REACHABLE and all(
            outcome.passed for outcome in self.denials
        ):
            return "passed"
        return "failed"

    def to_dict(self) -> dict[str, object]:
        return {
            "positive_control": self.positive_control.value,
            "denials": [outcome.to_dict() for outcome in self.denials],
        }

    @classmethod
    def from_dict(
        cls, raw: object, *, protocol_controls: frozenset[str] = frozenset()
    ) -> EgressPacketEvidence:
        """Rebuild one matrix, with the runtime's control contract supplied.

        ``protocol_controls`` is a keyword the CALLER knows from the runtime,
        never a field of ``raw``: the payload says what each leg observed, and
        the code says which legs were entitled to a control of their own.
        """
        if not isinstance(raw, Mapping):
            raise ValueError("egress packet evidence must be an object")
        if set(raw) != _PACKET_FIELDS:
            raise ValueError("egress packet evidence fields are malformed")
        denials = raw.get("denials")
        if not isinstance(denials, list):
            raise ValueError("egress packet denials must be a list")
        return cls(
            positive_control=PacketProbeStatus(str(raw.get("positive_control"))),
            denials=tuple(TopologyProbeResult.from_dict(item) for item in denials),
            protocol_controls=protocol_controls,
        )


def _require_pattern(pattern: re.Pattern[str], name: str, value: object) -> None:
    """Refuse a bound field that is not the exact shape it is matched on."""
    if not isinstance(value, str) or pattern.match(value) is None:
        raise ValueError(f"egress certification {name} is invalid")


def combined_verdict(packets: tuple[EgressPacketEvidence, ...]) -> str:
    """One verdict for several matrices, never better than the worst of them.

    ``unavailable`` wins over ``failed``, exactly as within one matrix: a
    binary kew could not read is not a binary observed to leak. ``passed``
    needs every matrix to have passed, and an empty set is not a pass.
    """
    verdicts = {packet.verdict for packet in packets}
    if not verdicts or "unavailable" in verdicts:
        return "unavailable"
    return "passed" if verdicts == {"passed"} else "failed"


@dataclass(frozen=True)
class RuntimeEgressCertification:
    """An observed egress matrix bound to one runtime's exact boundary.

    The five bound facts are the whole boundary: the immutable image the probe
    ran, the byte digest of the policy it ran under, the version of the control
    plane that enforced that policy, the identity of that gateway as it
    described itself, and the digest of the in-sandbox launcher the agent is
    started by — read inside the image, not off this host's package. Change any
    one of them and the proof no longer describes what a dispatch would run,
    which is why admission matches on the tuple rather than on the image alone.

    ``packets`` pairs every binary the policy governs with the matrix observed
    under that binary's own clause, in the policy's order.
    """

    schema_version: int
    runtime: str
    image_id: str
    policy_sha256: str
    gateway_version: str
    gateway_identity: str
    supervisor_digest: str
    packets: tuple[tuple[str, EgressPacketEvidence], ...]

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != CERTIFICATION_SCHEMA_VERSION
        ):
            raise ValueError("egress certification schema is unsupported")
        packets = tuple(self.packets)
        object.__setattr__(self, "packets", packets)
        if not packets:
            raise ValueError("egress certification probed no governed binary")
        binaries = [binary for binary, _ in packets]
        if len(set(binaries)) != len(binaries):
            raise ValueError("egress certification repeats a governed binary")
        for binary, packet in packets:
            _require_pattern(BINARY_PATH_RE, "binary", binary)
            self._require_complete_matrix(packet)
        _require_pattern(_IMAGE_ID_RE, "image_id", self.image_id)
        _require_pattern(_DIGEST_RE, "policy_sha256", self.policy_sha256)
        _require_pattern(_DIGEST_RE, "gateway_identity", self.gateway_identity)
        _require_pattern(_DIGEST_RE, "supervisor_digest", self.supervisor_digest)
        _require_pattern(_VERSION_RE, "gateway_version", self.gateway_version)

    def _require_complete_matrix(self, packet: object) -> None:
        """Refuse a matrix that is not this runtime's whole contract.

        Both halves of the contract: the denial names in canonical order, and
        the exact set of legs entitled to their own same-protocol control. A
        matrix that widened the second half could pass a leg on a control the
        probe never ran, so the runtime's constant is the only accepted value.
        """
        required = RUNTIME_PACKET_DENIALS.get(self.runtime)
        if required is None:
            raise ValueError(
                f"no egress certification contract for runtime {self.runtime!r}"
            )
        if not isinstance(packet, EgressPacketEvidence):
            raise ValueError("egress certification matrix is invalid")
        if packet.names != required:
            raise ValueError("egress certification requires the complete matrix")
        if packet.protocol_controls != RUNTIME_PACKET_CONTROLS[self.runtime]:
            raise ValueError("egress certification control contract is wrong")

    @property
    def binaries(self) -> tuple[str, ...]:
        """The governed binaries a matrix was observed for, in policy order."""
        return tuple(binary for binary, _ in self.packets)

    @property
    def verdict(self) -> str:
        """Every governed binary's matrix at once — see :func:`combined_verdict`."""
        return combined_verdict(tuple(packet for _, packet in self.packets))

    @property
    def binding(self) -> tuple[str, str, str, str, str]:
        """The exact tuple admission matches on."""
        return (
            self.image_id,
            self.policy_sha256,
            self.gateway_version,
            self.gateway_identity,
            self.supervisor_digest,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "runtime": self.runtime,
            "image_id": self.image_id,
            "policy_sha256": self.policy_sha256,
            "gateway_version": self.gateway_version,
            "gateway_identity": self.gateway_identity,
            "supervisor_digest": self.supervisor_digest,
            "packets": [
                {"binary": binary, "packet": packet.to_dict()}
                for binary, packet in self.packets
            ],
            "packet_verdict": self.verdict,
        }

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        )

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> RuntimeEgressCertification:
        if set(raw) != _CERTIFICATION_FIELDS:
            raise ValueError("egress certification fields are malformed")
        controls = RUNTIME_PACKET_CONTROLS.get(str(raw.get("runtime")), frozenset())
        entries = raw.get("packets")
        if not isinstance(entries, list):
            raise ValueError("egress certification packets must be a list")
        packets: list[tuple[str, EgressPacketEvidence]] = []
        for entry in entries:
            if not isinstance(entry, Mapping) or set(entry) != _BINARY_PACKET_FIELDS:
                raise ValueError("egress certification packet entry is malformed")
            packets.append(
                (
                    entry.get("binary"),  # type: ignore[arg-type]
                    EgressPacketEvidence.from_dict(
                        entry.get("packet"), protocol_controls=controls
                    ),
                )
            )
        verdict = combined_verdict(tuple(packet for _, packet in packets))
        if raw.get("packet_verdict") != verdict:
            raise ValueError("egress certification verdict is inconsistent")
        return cls(
            schema_version=raw.get("schema_version"),  # type: ignore[arg-type]
            runtime=raw.get("runtime"),  # type: ignore[arg-type]
            image_id=raw.get("image_id"),  # type: ignore[arg-type]
            policy_sha256=raw.get("policy_sha256"),  # type: ignore[arg-type]
            gateway_version=raw.get("gateway_version"),  # type: ignore[arg-type]
            gateway_identity=raw.get("gateway_identity"),  # type: ignore[arg-type]
            supervisor_digest=raw.get("supervisor_digest"),  # type: ignore[arg-type]
            packets=tuple(packets),
        )

    @classmethod
    def from_json(cls, raw: str) -> RuntimeEgressCertification:
        decoded = json.loads(raw)
        if not isinstance(decoded, dict):
            raise ValueError("egress certification JSON must be an object")
        return cls.from_dict(decoded)


def build_packet_evidence(
    *,
    positive_control: PacketProbeStatus,
    observations: Mapping[str, PacketProbeStatus],
    required: tuple[str, ...],
    controls: Mapping[str, PacketProbeStatus] | None = None,
) -> EgressPacketEvidence:
    """Assemble the matrix in canonical order, refusing an incomplete one.

    ``observations`` maps a denied target's name to what the probe saw from
    inside the sandbox. A name the prober never reported is NOT defaulted to a
    denial: the matrix is built from ``required``, so a missing entry raises
    here rather than certifying a destination nobody tested.

    ``controls`` maps a denied target to its OWN same-protocol positive control
    — the one the shared ``positive_control`` cannot stand in for because it
    speaks a different protocol. The same rule applies: a target listed in the
    runtime's control contract whose control is missing here raises rather than
    silently inheriting a control that proves nothing about it.
    """
    missing = [name for name in required if name not in observations]
    if missing:
        raise ValueError(f"egress probe reported no outcome for {', '.join(missing)}")
    unexpected = sorted(set(observations) - set(required))
    if unexpected:
        raise ValueError(
            f"egress probe reported unknown targets: {', '.join(unexpected)}"
        )
    observed_controls = dict(controls or {})
    stray = sorted(set(observed_controls) - set(required))
    if stray:
        raise ValueError(
            f"egress probe reported controls for unknown targets: {', '.join(stray)}"
        )
    return EgressPacketEvidence(
        positive_control=positive_control,
        denials=tuple(
            evaluate_topology_denial(
                name=name,
                positive_control=observed_controls.get(name, positive_control),
                worker=observations[name],
            )
            for name in required
        ),
        protocol_controls=frozenset(observed_controls),
    )


def bytes_digest(data: bytes) -> str:
    """SHA-256 of bytes already read — how every bound digest here is computed.

    Callers that go on to USE the bytes hash them through this, never through a
    second read of the file: a digest of one read and a launch from another is
    a proof about bytes that never ran (#2624).
    """
    return hashlib.sha256(data).hexdigest()


def file_digest(path: Path) -> str:
    """SHA-256 of one file's bytes, for a file that is only matched, not used.

    One function so the policy digest the prober records and the one admission
    matches it against cannot be computed two different ways. The files are a
    YAML policy and a shell launcher, so they are read whole.
    """
    return bytes_digest(path.read_bytes())
