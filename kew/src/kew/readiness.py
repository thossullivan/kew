"""Pure composition of capability-aware route profiles, and host readiness.

The host-readiness section at the end of this module is the structured result
``kew doctor`` renders as prose or as ``--json``. The checks that produce it
live in ``kew.cli.doctor`` (``run_checks``); this module holds only the shape,
because the dispatch stack imports it and must never import the CLI.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, fields, replace
from enum import StrEnum
from types import MappingProxyType
from typing import ClassVar, Final

from kew.credential_surface import redact_credential_values
from kew.inference_transport import ResolvedInferenceTransport
from kew.runners.capabilities import capability_manifest


class Provenance(StrEnum):
    DECLARED = "declared"
    ENFORCED = "enforced"
    OBSERVED = "observed"
    MEASURED = "measured"
    UNAVAILABLE = "unavailable"


class PacketProbeStatus(StrEnum):
    """One observed packet result from one network namespace."""

    REACHABLE = "reachable"
    DENIED = "denied"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class TopologyProbeResult:
    """One positive-control and worker packet observation pair."""

    name: str
    positive_control: PacketProbeStatus
    worker: PacketProbeStatus
    passed: bool
    reason: str | None = None

    def to_dict(self) -> dict[str, object | None]:
        return {
            "name": self.name,
            "positive_control": self.positive_control.value,
            "worker": self.worker.value,
            "passed": self.passed,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, raw: object) -> TopologyProbeResult:
        if not isinstance(raw, Mapping):
            raise ValueError("packet outcome must be an object")
        if set(raw) != {
            "name",
            "positive_control",
            "worker",
            "passed",
            "reason",
        }:
            raise ValueError("packet outcome fields are malformed")
        name = raw.get("name")
        control = raw.get("positive_control")
        worker = raw.get("worker")
        passed = raw.get("passed")
        reason = raw.get("reason")
        if not isinstance(name, str) or not name:
            raise ValueError("packet outcome requires a name")
        if type(passed) is not bool:
            raise ValueError("packet outcome requires a Boolean verdict")
        if reason is not None and not isinstance(reason, str):
            raise ValueError("packet outcome reason must be a string")
        return cls(
            name=name,
            positive_control=PacketProbeStatus(str(control)),
            worker=PacketProbeStatus(str(worker)),
            passed=passed,
            reason=reason,
        )


def evaluate_topology_denial(
    *,
    name: str,
    positive_control: PacketProbeStatus,
    worker: PacketProbeStatus,
) -> TopologyProbeResult:
    """Pass only a proven listener followed by an observed worker denial."""
    if not isinstance(positive_control, PacketProbeStatus):
        raise ValueError("positive_control must be a PacketProbeStatus")
    if not isinstance(worker, PacketProbeStatus):
        raise ValueError("worker must be a PacketProbeStatus")
    if positive_control is PacketProbeStatus.UNAVAILABLE:
        reason = "positive_control_unavailable"
    elif positive_control is not PacketProbeStatus.REACHABLE:
        reason = "positive_control_failed"
    elif worker is PacketProbeStatus.UNAVAILABLE:
        reason = "worker_probe_unavailable"
    elif worker is PacketProbeStatus.REACHABLE:
        reason = "packet_leak"
    elif worker is PacketProbeStatus.DENIED:
        reason = None
    else:  # pragma: no cover - exhaustive after the status-domain check
        raise ValueError("worker must be a PacketProbeStatus")
    return TopologyProbeResult(
        name=name,
        positive_control=positive_control,
        worker=worker,
        passed=reason is None,
        reason=reason,
    )


SIDECAR_PACKET_DENIALS: Final[tuple[str, ...]] = (
    "egress_peer",
    "host_gateway",
    "macos_host",
    "external",
    "udp",
    "dns",
    "ipv6",
    "extra_proxy_port",
)
"""The complete SC-0 negative packet matrix, in canonical order."""


@dataclass(frozen=True)
class SidecarPacketEvidence:
    """The complete positive and negative packet observations for one proof."""

    proxy_health: PacketProbeStatus
    proxy_route: PacketProbeStatus
    denials: tuple[TopologyProbeResult, ...]

    def __post_init__(self) -> None:
        denials = tuple(self.denials)
        names = tuple(outcome.name for outcome in denials)
        if names != SIDECAR_PACKET_DENIALS:
            raise ValueError("sidecar packet evidence requires the complete matrix")
        for outcome in denials:
            expected = evaluate_topology_denial(
                name=outcome.name,
                positive_control=outcome.positive_control,
                worker=outcome.worker,
            )
            if outcome != expected:
                raise ValueError("sidecar packet outcome verdict is inconsistent")
        object.__setattr__(self, "denials", denials)

    @property
    def verdict(self) -> str:
        statuses = (
            self.proxy_health,
            self.proxy_route,
            *(outcome.positive_control for outcome in self.denials),
            *(outcome.worker for outcome in self.denials),
        )
        if PacketProbeStatus.UNAVAILABLE in statuses:
            return "unavailable"
        if (
            self.proxy_health is PacketProbeStatus.REACHABLE
            and self.proxy_route is PacketProbeStatus.REACHABLE
            and all(outcome.passed for outcome in self.denials)
        ):
            return "passed"
        return "failed"

    def to_dict(self) -> dict[str, object]:
        return {
            "proxy_health": self.proxy_health.value,
            "proxy_route": self.proxy_route.value,
            "denials": [outcome.to_dict() for outcome in self.denials],
        }

    @classmethod
    def from_dict(cls, raw: object) -> SidecarPacketEvidence:
        if not isinstance(raw, Mapping):
            raise ValueError("sidecar packet evidence must be an object")
        if set(raw) != {"proxy_health", "proxy_route", "denials"}:
            raise ValueError("sidecar packet evidence fields are malformed")
        denials = raw.get("denials")
        if not isinstance(denials, list):
            raise ValueError("sidecar packet denials must be a list")
        return cls(
            proxy_health=PacketProbeStatus(str(raw.get("proxy_health"))),
            proxy_route=PacketProbeStatus(str(raw.get("proxy_route"))),
            denials=tuple(TopologyProbeResult.from_dict(item) for item in denials),
        )


def sidecar_packet_policy_sha256() -> str:
    """Bind certification to the generated NF-1 and internal-network contract."""
    from kew.sandbox import (
        build_per_run_network_argv,
        build_worker_netns_filter_helper_argv,
        worker_netns_filter_ruleset,
    )

    owner = "0" * 32
    material = {
        "denials": SIDECAR_PACKET_DENIALS,
        "network": build_per_run_network_argv(
            runtime="docker", network="kew-packet-policy", owner=owner
        ),
        "helper": build_worker_netns_filter_helper_argv(
            runtime="docker",
            image_id="sha256:" + "0" * 64,
            worker_id="0" * 64,
            owner=owner,
        ),
        "ruleset": worker_netns_filter_ruleset(
            owner=owner, proxy_address="192.0.2.1", proxy_port=4000
        ).decode("ascii"),
    }
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@dataclass(frozen=True)
class SidecarPacketCertification:
    """An operator-observed SC-0 matrix bound to immutable runtime images."""

    schema_version: int
    worker_image_id: str
    proxy_image_id: str
    packet: SidecarPacketEvidence
    filter_policy_sha256: str = field(default_factory=sidecar_packet_policy_sha256)

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("sidecar packet certification schema is unsupported")
        for name, value in (
            ("worker_image_id", self.worker_image_id),
            ("proxy_image_id", self.proxy_image_id),
        ):
            if re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None:
                raise ValueError(f"sidecar packet certification {name} is invalid")
        if not isinstance(self.packet, SidecarPacketEvidence):
            raise ValueError("sidecar packet certification matrix is invalid")
        if re.fullmatch(r"[0-9a-f]{64}", self.filter_policy_sha256) is None:
            raise ValueError("sidecar packet certification policy is invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "worker_image_id": self.worker_image_id,
            "proxy_image_id": self.proxy_image_id,
            "filter_policy_sha256": self.filter_policy_sha256,
            "packet": self.packet.to_dict(),
            "packet_verdict": self.packet.verdict,
        }

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        )

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> SidecarPacketCertification:
        if set(raw) != {
            "schema_version",
            "worker_image_id",
            "proxy_image_id",
            "filter_policy_sha256",
            "packet",
            "packet_verdict",
        }:
            raise ValueError("sidecar packet certification fields are malformed")
        packet = SidecarPacketEvidence.from_dict(raw.get("packet"))
        if raw.get("packet_verdict") != packet.verdict:
            raise ValueError("sidecar packet certification verdict is inconsistent")
        return cls(
            schema_version=raw.get("schema_version"),  # type: ignore[arg-type]
            worker_image_id=raw.get("worker_image_id"),  # type: ignore[arg-type]
            proxy_image_id=raw.get("proxy_image_id"),  # type: ignore[arg-type]
            filter_policy_sha256=raw.get("filter_policy_sha256"),  # type: ignore[arg-type]
            packet=packet,
        )

    @classmethod
    def from_json(cls, raw: str) -> SidecarPacketCertification:
        decoded = json.loads(raw)
        if not isinstance(decoded, dict):
            raise ValueError("sidecar packet certification JSON must be an object")
        return cls.from_dict(decoded)


_SIDECAR_ID_RE: Final = re.compile(r"^[0-9a-f]{64}$")
_SIDECAR_OWNER_RE: Final = re.compile(r"^[0-9a-f]{32}$")
_SIDECAR_TOKEN_RE: Final = re.compile(r"^[A-Za-z0-9_.:/+-]{1,256}$")
_SIDECAR_STAGES: Final = frozenset({"admission", "terminal"})
_SIDECAR_FILTER_VERDICTS: Final = frozenset({"passed", "failed", "unavailable"})
_SIDECAR_CLEANUP_VERDICTS: Final = frozenset(
    {"pending", "passed", "failed", "unavailable"}
)
_SIDECAR_EVIDENCE_FIELDS: Final = frozenset(
    {
        "schema_version",
        "stage",
        "run_id",
        "attempt_number",
        "profile_fingerprint",
        "route",
        "config_sha256",
        "owner",
        "worker_image_id",
        "proxy_image_id",
        "network_id",
        "egress_network_id",
        "proxy_id",
        "worker_id",
        "config_helper_id",
        "filter_helper_id",
        "config_storage",
        "endpoint",
        "observed_version",
        "packet",
        "packet_verdict",
        "filter_verdict",
        "cleanup_verdict",
        "packet_source",
        "packet_policy_sha256",
    }
)


@dataclass(frozen=True)
class SidecarTopologyEvidence:
    """Immutable, secret-free isolation evidence for one sidecar attempt."""

    schema_version: int
    stage: str
    run_id: str
    attempt_number: int
    profile_fingerprint: str
    route: str
    config_sha256: str
    owner: str
    worker_image_id: str
    proxy_image_id: str
    network_id: str
    egress_network_id: str
    proxy_id: str
    worker_id: str
    config_helper_id: str
    filter_helper_id: str
    config_storage: str
    endpoint: str
    observed_version: str
    packet: SidecarPacketEvidence
    filter_verdict: str
    cleanup_verdict: str
    packet_policy_sha256: str = field(default_factory=sidecar_packet_policy_sha256)

    def __post_init__(self) -> None:
        from kew.switchyard import sanitize_endpoint

        self._check_identity_fields()
        endpoint = sanitize_endpoint(self.endpoint)
        if endpoint == "invalid-endpoint":
            raise ValueError("sidecar evidence endpoint is invalid")
        object.__setattr__(self, "endpoint", endpoint)
        self._check_verdict_fields()

    def _check_stage_fields(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("sidecar evidence schema is unsupported")
        if self.stage not in _SIDECAR_STAGES:
            raise ValueError("sidecar evidence stage is invalid")
        if type(self.attempt_number) is not int or self.attempt_number < 1:
            raise ValueError("sidecar evidence attempt number is invalid")

    def _check_identity_fields(self) -> None:
        """Validate the immutable attempt, resource, and policy identities."""
        self._check_stage_fields()
        for name, value in (
            ("run_id", self.run_id),
            ("route", self.route),
            ("config_storage", self.config_storage),
            ("observed_version", self.observed_version),
        ):
            if _SIDECAR_TOKEN_RE.fullmatch(value) is None:
                raise ValueError(f"sidecar evidence {name} is invalid")
        for name, value in (
            ("profile_fingerprint", self.profile_fingerprint),
            ("config_sha256", self.config_sha256),
            ("network_id", self.network_id),
            ("egress_network_id", self.egress_network_id),
            ("proxy_id", self.proxy_id),
            ("worker_id", self.worker_id),
            ("config_helper_id", self.config_helper_id),
            ("filter_helper_id", self.filter_helper_id),
            ("packet_policy_sha256", self.packet_policy_sha256),
        ):
            if _SIDECAR_ID_RE.fullmatch(value) is None:
                raise ValueError(f"sidecar evidence {name} is invalid")
        if _SIDECAR_OWNER_RE.fullmatch(self.owner) is None:
            raise ValueError("sidecar evidence owner is invalid")
        for name, value in (
            ("worker_image_id", self.worker_image_id),
            ("proxy_image_id", self.proxy_image_id),
        ):
            if re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None:
                raise ValueError(f"sidecar evidence {name} is invalid")

    def _check_verdict_fields(self) -> None:
        """Keep certification, live filtering, and terminal cleanup distinct."""
        if not isinstance(self.packet, SidecarPacketEvidence):
            raise ValueError("sidecar packet evidence is invalid")
        if self.filter_verdict not in _SIDECAR_FILTER_VERDICTS:
            raise ValueError("sidecar filter verdict is invalid")
        if self.cleanup_verdict not in _SIDECAR_CLEANUP_VERDICTS:
            raise ValueError("sidecar cleanup verdict is invalid")
        if self.stage == "admission" and self.cleanup_verdict != "pending":
            raise ValueError("sidecar admission cleanup must be pending")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "stage": self.stage,
            "run_id": self.run_id,
            "attempt_number": self.attempt_number,
            "profile_fingerprint": self.profile_fingerprint,
            "route": self.route,
            "config_sha256": self.config_sha256,
            "owner": self.owner,
            "worker_image_id": self.worker_image_id,
            "proxy_image_id": self.proxy_image_id,
            "network_id": self.network_id,
            "egress_network_id": self.egress_network_id,
            "proxy_id": self.proxy_id,
            "worker_id": self.worker_id,
            "config_helper_id": self.config_helper_id,
            "filter_helper_id": self.filter_helper_id,
            "config_storage": self.config_storage,
            "endpoint": self.endpoint,
            "observed_version": self.observed_version,
            "packet": self.packet.to_dict(),
            "packet_verdict": self.packet.verdict,
            "filter_verdict": self.filter_verdict,
            "cleanup_verdict": self.cleanup_verdict,
            "packet_source": "operator-certification",
            "packet_policy_sha256": self.packet_policy_sha256,
        }

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        )

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> SidecarTopologyEvidence:
        if set(raw) != _SIDECAR_EVIDENCE_FIELDS:
            raise ValueError("sidecar evidence fields are malformed")
        if raw.get("packet_source") != "operator-certification":
            raise ValueError("sidecar packet source is invalid")
        fields = {
            name: raw.get(name)
            for name in (
                "schema_version",
                "stage",
                "run_id",
                "attempt_number",
                "profile_fingerprint",
                "route",
                "config_sha256",
                "owner",
                "worker_image_id",
                "proxy_image_id",
                "network_id",
                "egress_network_id",
                "proxy_id",
                "worker_id",
                "config_helper_id",
                "filter_helper_id",
                "config_storage",
                "endpoint",
                "observed_version",
                "filter_verdict",
                "cleanup_verdict",
                "packet_policy_sha256",
            )
        }
        if not all(isinstance(value, (str, int)) for value in fields.values()):
            raise ValueError("sidecar evidence fields are malformed")
        packet = SidecarPacketEvidence.from_dict(raw.get("packet"))
        if raw.get("packet_verdict") != packet.verdict:
            raise ValueError("sidecar packet verdict is inconsistent")
        return cls(
            **fields,  # type: ignore[arg-type]
            packet=packet,
        )

    @classmethod
    def from_json(cls, raw: str) -> SidecarTopologyEvidence:
        decoded = json.loads(raw)
        if not isinstance(decoded, dict):
            raise ValueError("sidecar evidence JSON must be an object")
        return cls.from_dict(decoded)

    def binding(self) -> tuple[object, ...]:
        """Return facts that admission and terminal evidence must share."""
        payload = self.to_dict()
        payload.pop("stage")
        payload.pop("cleanup_verdict")
        return tuple((key, _freeze_value(value)) for key, value in payload.items())


def _freeze_value(value: object) -> object:
    """Copy JSON-shaped data into immutable containers."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return _freeze_float(value)
    if isinstance(value, Mapping):
        return _freeze_mapping(value)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_value(item) for item in value)
    raise ValueError("profile values must be JSON-shaped")


def _freeze_float(value: float) -> float:
    try:
        json.dumps(value, allow_nan=False)
    except ValueError as exc:
        raise ValueError("profile values must be finite JSON values") from exc
    return value


def _freeze_mapping(value: Mapping[object, object]) -> Mapping[str, object]:
    if not all(isinstance(key, str) for key in value):
        raise ValueError("profile object keys must be strings")
    return MappingProxyType(
        {str(key): _freeze_value(item) for key, item in value.items()}
    )


def _thaw_value(value: object) -> object:
    """Return ordinary JSON containers for serialization and persistence."""
    if isinstance(value, Mapping):
        return {str(key): _thaw_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_value(item) for item in value]
    return value


@dataclass(frozen=True)
class ProfileFact:
    """One profile value and the owner/evidence by which it is known."""

    value: object | None
    provenance: Provenance
    source: str

    def __post_init__(self) -> None:
        if self.provenance is Provenance.UNAVAILABLE and self.value is not None:
            raise ValueError("unavailable profile facts cannot carry a value")
        if self.provenance is Provenance.UNAVAILABLE and self.source:
            raise ValueError("unavailable profile facts cannot claim a source")
        if self.provenance is not Provenance.UNAVAILABLE and not self.source.strip():
            raise ValueError("available profile facts require a source")
        object.__setattr__(self, "value", _freeze_value(self.value))

    @classmethod
    def unavailable(cls) -> ProfileFact:
        return cls(value=None, provenance=Provenance.UNAVAILABLE, source="")

    def to_dict(self) -> dict[str, object | None]:
        return {
            "value": _thaw_value(self.value),
            "provenance": self.provenance.value,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, raw: object) -> ProfileFact:
        if not isinstance(raw, Mapping):
            return cls.unavailable()
        try:
            provenance = Provenance(str(raw.get("provenance", "unavailable")))
        except ValueError:
            return cls.unavailable()
        value = raw.get("value")
        source = raw.get("source", "")
        if not isinstance(source, str):
            return cls.unavailable()
        try:
            return cls(value=value, provenance=provenance, source=source)
        except ValueError:
            return cls.unavailable()


@dataclass(frozen=True)
class ResolvedRouteProfile:
    """Immutable configured profile for one resolved route.

    Realized executable/image and admission evidence are intentionally absent;
    the following slice captures those at the launch boundary.
    """

    schema_version: int
    runner: str
    requested_model: str
    resume: ProfileFact
    telemetry: ProfileFact
    cost: ProfileFact
    loop: ProfileFact
    containment_requirement: ProfileFact
    transport: ProfileFact
    containment: ProfileFact
    tracker_workflow: ProfileFact
    verification: ProfileFact
    independent_review: ProfileFact
    publication: ProfileFact
    metering: ProfileFact
    exclusions: ProfileFact

    RUNNER_FACT_FIELDS: ClassVar[tuple[str, ...]] = (
        "resume",
        "telemetry",
        "cost",
        "loop",
        "containment_requirement",
    )
    ROUTE_FACT_FIELDS: ClassVar[tuple[str, ...]] = (
        "transport",
        "containment",
        "tracker_workflow",
        "verification",
        "independent_review",
        "publication",
        "metering",
        "exclusions",
    )
    ALL_FACT_FIELDS: ClassVar[tuple[str, ...]] = (
        *RUNNER_FACT_FIELDS,
        *ROUTE_FACT_FIELDS,
    )

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "schema_version": self.schema_version,
            "runner": self.runner,
            "requested_model": self.requested_model,
        }
        result.update(
            {name: getattr(self, name).to_dict() for name in self.ALL_FACT_FIELDS}
        )
        return result

    def to_json(self) -> str:
        """Serialize canonically for durable profile retention."""
        return json.dumps(
            self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        )

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> ResolvedRouteProfile:
        version = raw.get("schema_version", 0)
        runner = raw.get("runner", "")
        requested_model = raw.get("requested_model", "")
        legacy = version != 1
        facts = {
            name: ProfileFact.unavailable()
            if legacy
            else ProfileFact.from_dict(raw.get(name))
            for name in cls.ALL_FACT_FIELDS
        }
        return cls(
            schema_version=1 if version == 1 else 0,
            runner=runner if isinstance(runner, str) else "",
            requested_model=requested_model if isinstance(requested_model, str) else "",
            **facts,
        )

    @classmethod
    def from_json(cls, raw: str) -> ResolvedRouteProfile:
        """Restore a retained profile, rejecting non-object payloads."""
        decoded = json.loads(raw)
        if not isinstance(decoded, dict):
            raise ValueError("route profile JSON must be an object")
        return cls.from_dict(decoded)

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self.to_json().encode()).hexdigest()


def _transport_fact(transport: ResolvedInferenceTransport | None) -> ProfileFact:
    if transport is None:
        return ProfileFact.unavailable()
    value = {
        field.name: getattr(transport, field.name)
        for field in fields(transport)
        if field.name != "credential_env_names"
    }
    # Credential names are auth-path identity, not secrets, but admission owns
    # their capture in the next slice; do not accidentally retain them here.
    return ProfileFact(
        value=value,
        provenance=Provenance.ENFORCED,
        source="inference transport resolver",
    )


def compose_route_profile(
    *,
    runner: str,
    requested_model: str,
    transport: ResolvedInferenceTransport | ProfileFact | None = None,
    containment: ProfileFact | None = None,
    tracker_workflow: ProfileFact | None = None,
    verification: ProfileFact | None = None,
    independent_review: ProfileFact | None = None,
    publication: ProfileFact | None = None,
    metering: ProfileFact | None = None,
    exclusions: ProfileFact | None = None,
) -> ResolvedRouteProfile:
    """Compose declarations with facts supplied by their existing owners."""
    native = capability_manifest(runner)

    def declared(value: object) -> ProfileFact:
        return ProfileFact(value, Provenance.DECLARED, "runner capability manifest")

    route_transport = (
        transport if isinstance(transport, ProfileFact) else _transport_fact(transport)
    )
    unknown = ProfileFact.unavailable
    return ResolvedRouteProfile(
        schema_version=1,
        runner=runner,
        requested_model=requested_model,
        resume=declared(native.resume),
        telemetry=declared(list(native.telemetry)),
        cost=declared(native.cost.value),
        loop=declared(native.loop),
        containment_requirement=declared(native.containment.value),
        transport=route_transport,
        containment=containment or unknown(),
        tracker_workflow=tracker_workflow or unknown(),
        verification=verification or unknown(),
        independent_review=independent_review or unknown(),
        publication=publication or unknown(),
        metering=metering or unknown(),
        exclusions=exclusions or unknown(),
    )


class Containment(StrEnum):
    """The boundary one paid attempt actually launched across.

    ``NATIVE`` is a runner that contains itself (its own sandbox); ``HOST`` is
    an ordinary host launch; ``UNCONTAINED`` is the operator's explicit
    ``dangerously_skip_containment`` — a distinct fact that must never be
    filed under plain host execution.
    """

    HOST = "host"
    CONTAINED = "contained"
    NATIVE = "native"
    UNCONTAINED = "uncontained"
    UNKNOWN = "unknown"


WORK_TRACKERS: Final[frozenset[str]] = frozenset({"github", "linear"})
"""Trackers that may own a run's SOURCE identity — mirrors ``KNOWN_TRACKERS``."""

INFERENCE_PROVIDERS: Final[frozenset[str]] = frozenset(
    {"anthropic", "openai", "google", "openrouter", "local"}
)
"""Names that identify an INFERENCE provider and therefore never a tracker."""

_IMAGE_ID_RE: Final = re.compile(r"^(?:[a-z0-9][a-z0-9+.-]*:)?[0-9a-f]{32,}$")
_DIGEST_RE: Final = re.compile(r"^[0-9a-f]{64}$")
_AUTH_PATH_RE: Final = re.compile(r"^[A-Za-z0-9_.:@/+-]{1,128}$")
_VERSION_RE: Final = re.compile(r"^[A-Za-z0-9_.:+-]{1,64}$")


class RealizedProfileError(ValueError):
    """A realized launch boundary is not internally honest."""


@dataclass(frozen=True)
class RealizedLaunchBoundary:
    """The exact boundary resolved immediately before one paid child launch.

    Every field is "the fact as resolved, or not known" — never a placeholder
    and never a guess. The constructor is the fail-closed gate the guardrails
    demand: a contained attempt cannot be described by a mutable tag, a host
    attempt cannot borrow an image identity, and the WORK tracker can never be
    filled in with the name of an INFERENCE provider.
    """

    containment: Containment
    source_provider: str | None = None
    inference_provider: str | None = None
    runner_executable: str | None = None
    runner_version: str | None = None
    runner_digest: str | None = None
    image_ref: str | None = None
    image_id: str | None = None
    endpoint: str | None = None
    service_tier: str | None = None
    auth_path: str | None = None

    def __post_init__(self) -> None:
        self._check_providers()
        self._check_material_shapes()
        self._check_containment()

    def _check_providers(self) -> None:
        """Keep the tracker column a tracker and the provider column a provider."""
        tracker = self.source_provider
        if tracker is not None:
            if tracker in INFERENCE_PROVIDERS:
                raise RealizedProfileError(
                    f"source provider {tracker!r} is an inference provider, "
                    "not a work tracker"
                )
            if tracker not in WORK_TRACKERS:
                raise RealizedProfileError(f"unknown work tracker {tracker!r}")
        if self.inference_provider is not None and (
            self.inference_provider in WORK_TRACKERS
        ):
            raise RealizedProfileError(
                f"inference provider {self.inference_provider!r} names a work tracker"
            )

    def _check_material_shapes(self) -> None:
        """Refuse identity material that is not the shape it claims to be."""
        checks = (
            ("runner_digest", self.runner_digest, _DIGEST_RE, "a SHA-256 digest"),
            ("image_id", self.image_id, _IMAGE_ID_RE, "an immutable image ID"),
            ("auth_path", self.auth_path, _AUTH_PATH_RE, "an auth-path identifier"),
            ("runner_version", self.runner_version, _VERSION_RE, "a version string"),
        )
        for name, value, pattern, expected in checks:
            if value is not None and pattern.fullmatch(value) is None:
                raise RealizedProfileError(f"{name} must be {expected}")

    def _check_containment(self) -> None:
        """Bind the identity a boundary is allowed to claim to what it resolved."""
        if self.containment is Containment.HOST and (
            self.image_ref is not None or self.image_id is not None
        ):
            raise RealizedProfileError("a host attempt has no container image")
        if self.containment is Containment.CONTAINED and self.image_id is None:
            raise RealizedProfileError(
                "a contained attempt requires a resolved image ID — "
                "a mutable tag is not an immutable boundary"
            )
        if self.containment is Containment.UNKNOWN and (
            self.image_id is not None or self.runner_digest is not None
        ):
            raise RealizedProfileError(
                "an unresolved boundary cannot claim a resolved identity"
            )


_BOUNDARY_FACTS: Final[tuple[tuple[str, Provenance, str], ...]] = (
    ("source_provider", Provenance.DECLARED, "work tracker adapter"),
    ("inference_provider", Provenance.DECLARED, "route provider inference"),
    ("runner_executable", Provenance.OBSERVED, "runner executable probe"),
    ("runner_version", Provenance.OBSERVED, "runner executable probe"),
    ("runner_digest", Provenance.MEASURED, "runner executable probe"),
    ("image_ref", Provenance.DECLARED, "sandbox image mapping"),
    ("image_id", Provenance.OBSERVED, "container image inspect"),
    ("endpoint", Provenance.ENFORCED, "inference transport resolver"),
    ("service_tier", Provenance.DECLARED, "route service tier"),
    ("auth_path", Provenance.ENFORCED, "credential resolution"),
)


@dataclass(frozen=True)
class RealizedRouteProfile:
    """The immutable profile of one paid attempt, bound to that attempt.

    ``route`` is the configured profile I03 composes; the remaining facts are
    what the launch boundary actually resolved. The pair is fingerprinted
    together, so either a route/config change or a boundary change (a moved
    tag, a rebuilt image, a different executable) yields a different
    fingerprint.

    Nothing here is per-run mutable state: later observations are appended
    beside a profile, never folded into it.
    """

    schema_version: int
    run_id: str
    attempt_number: int
    route: ResolvedRouteProfile
    containment: ProfileFact
    source_provider: ProfileFact
    inference_provider: ProfileFact
    runner_executable: ProfileFact
    runner_version: ProfileFact
    runner_digest: ProfileFact
    image_ref: ProfileFact
    image_id: ProfileFact
    endpoint: ProfileFact
    service_tier: ProfileFact
    auth_path: ProfileFact
    credential_issuer: ProfileFact = field(default_factory=ProfileFact.unavailable)

    REALIZED_FACT_FIELDS: ClassVar[tuple[str, ...]] = (
        "containment",
        *(name for name, _, _ in _BOUNDARY_FACTS),
        "credential_issuer",
    )

    @property
    def runner(self) -> str:
        return self.route.runner

    @property
    def requested_model(self) -> str:
        return self.route.requested_model

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "attempt_number": self.attempt_number,
            "route": self.route.to_dict(),
        }
        result.update(
            {name: getattr(self, name).to_dict() for name in self.REALIZED_FACT_FIELDS}
        )
        # Preserve fingerprints for profiles recorded before bounded issuance.
        if self.credential_issuer.value is None:
            result.pop("credential_issuer", None)
        return result

    def to_json(self) -> str:
        """Serialize the whole row canonically for durable retention."""
        return json.dumps(
            self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        )

    def material_json(self) -> str:
        """The fingerprinted material: every fact EXCEPT run/attempt identity.

        Identical executable/image/model/endpoint material must share one
        fingerprint across runs and attempts — that comparison is what
        readiness and certification reuse are built on. Row identity lives in
        the table keys, not in the material.
        """
        material = self.to_dict()
        material.pop("run_id", None)
        material.pop("attempt_number", None)
        return json.dumps(
            material, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        )

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> RealizedRouteProfile:
        legacy = raw.get("schema_version") != 1
        route_raw = raw.get("route")
        route = ResolvedRouteProfile.from_dict(
            route_raw if isinstance(route_raw, Mapping) and not legacy else {}
        )
        attempt = raw.get("attempt_number", 1)
        run_id = raw.get("run_id", "")
        facts = {
            name: ProfileFact.unavailable()
            if legacy
            else ProfileFact.from_dict(raw.get(name))
            for name in cls.REALIZED_FACT_FIELDS
        }
        return cls(
            schema_version=0 if legacy else 1,
            run_id=run_id if isinstance(run_id, str) else "",
            attempt_number=attempt if isinstance(attempt, int) else 1,
            route=route,
            **facts,
        )

    @classmethod
    def from_json(cls, raw: str) -> RealizedRouteProfile:
        decoded = json.loads(raw)
        if not isinstance(decoded, dict):
            raise ValueError("realized route profile JSON must be an object")
        return cls.from_dict(decoded)

    def column_values(self) -> Mapping[str, str | None]:
        """The persistence-facing column mapping of this profile.

        Flattens each fact to its plain value (``None`` where unavailable) so
        the store can retain a queryable projection without importing — or
        interpreting — this module. The canonical record stays
        :meth:`to_json`; these are an index over it.
        """
        columns: dict[str, str | None] = {
            "runner": self.runner,
            "requested_model": self.requested_model,
        }
        for name in self.REALIZED_FACT_FIELDS:
            value = getattr(self, name).value
            columns[name] = None if value is None else str(value)
        return columns

    @classmethod
    def restore(
        cls, profile_json: str | None, run_id: str, attempt_number: int = 1
    ) -> RealizedRouteProfile:
        """Interpret a retained payload, or answer explicit unknowns without one.

        ``None`` (a run admitted before profiles existed) and an unreadable
        payload get the SAME answer: all facts unavailable. Neither is
        reconstructed from whatever columns happen to sit beside it — that
        reconstruction is exactly the borrowed evidence this profile refuses.
        """
        if profile_json is None:
            return cls.unknown(run_id, attempt_number)
        try:
            return cls.from_json(profile_json)
        except (ValueError, TypeError):
            return cls.unknown(run_id, attempt_number)

    @classmethod
    def unknown(cls, run_id: str, attempt_number: int = 1) -> RealizedRouteProfile:
        """The explicit all-unknown profile a legacy (unprofiled) attempt has."""
        return cls.from_dict(
            {"schema_version": 0, "run_id": run_id, "attempt_number": attempt_number}
        )

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self.material_json().encode()).hexdigest()


def _boundary_fact(
    boundary: RealizedLaunchBoundary, name: str, provenance: Provenance, source: str
) -> ProfileFact:
    value = getattr(boundary, name)
    if value is None:
        return ProfileFact.unavailable()
    return ProfileFact(value=value, provenance=provenance, source=source)


def _containment_fact(boundary: RealizedLaunchBoundary) -> ProfileFact:
    """The enforced boundary, or an explicit unknown when nothing resolved."""
    if boundary.containment is Containment.UNKNOWN:
        return ProfileFact.unavailable()
    return ProfileFact(
        value=boundary.containment.value,
        provenance=Provenance.ENFORCED,
        source="sandbox decision",
    )


def compose_realized_profile(
    *,
    run_id: str,
    attempt_number: int,
    route: ResolvedRouteProfile,
    boundary: RealizedLaunchBoundary,
    credential_issuer: Mapping[str, object] | None = None,
) -> RealizedRouteProfile:
    """Bind a configured route profile to the boundary one attempt launched on.

    The configured profile's ``containment`` fact is overwritten with the
    enforced boundary rather than trusted from the caller: two disagreeing
    answers to "was this contained" is exactly the ambiguity this profile
    exists to end.
    """
    if attempt_number < 1:
        raise RealizedProfileError("attempt numbers start at 1")
    if not run_id:
        raise RealizedProfileError("a realized profile requires a run id")
    containment = _containment_fact(boundary)
    facts = {
        name: _boundary_fact(boundary, name, provenance, source)
        for name, provenance, source in _BOUNDARY_FACTS
    }
    return RealizedRouteProfile(
        schema_version=1,
        run_id=run_id,
        attempt_number=attempt_number,
        route=replace(route, containment=containment),
        containment=containment,
        credential_issuer=(
            ProfileFact.unavailable()
            if credential_issuer is None
            else ProfileFact(
                credential_issuer, Provenance.ENFORCED, "issuer bounded issuance"
            )
        ),
        **facts,
    )


READINESS_PROBE_RUN_KIND: Final = "readiness_probe"
"""``RunRecord.run_kind`` of the one operational row a readiness probe pays on.

Mirrored as a literal in :data:`kew.run_store.READINESS_PROBE_RUN_KIND` — the
store sits below :mod:`kew.runners` (which this module imports) and so cannot
import it from here. ``tests/test_run_store.py`` guards the two against drift.
"""

_MONEY_CERTAINTIES: Final = frozenset(
    {Provenance.DECLARED, Provenance.MEASURED, Provenance.UNAVAILABLE}
)


@dataclass(frozen=True)
class ProbeCost:
    """What a readiness probe cost, where the number came from, and how sure.

    Money is the one profile fact that must never quietly become a guess, so
    the amount travels with both halves of its evidence:

    * ``source`` — *who says so*: the planned ceiling the doctor action bounds
      itself by, or the runner's own reported usage.
    * ``certainty`` — *how well it is known*: ``DECLARED`` for a planned bound
      kew asserted up front, ``MEASURED`` for spend an execution reported back,
      ``UNAVAILABLE`` when nothing is known at all.

    ``OBSERVED``/``ENFORCED`` are deliberately not money certainties: they
    describe boundary facts kew watched or imposed, and reusing them here would
    make "$0.02 observed" ambiguous between "the runner reported it" and "we
    saw the container we launched".
    """

    amount_usd: float
    source: str
    certainty: Provenance

    def __post_init__(self) -> None:
        if self.certainty not in _MONEY_CERTAINTIES:
            raise ValueError(f"{self.certainty} is not a cost certainty")
        self._check_amount()
        if self.certainty is Provenance.UNAVAILABLE:
            if self.amount_usd or self.source:
                raise ValueError("an unknown probe cost carries no amount or source")
        elif not self.source.strip():
            raise ValueError("a known probe cost must name its source")

    def _check_amount(self) -> None:
        amount = float(self.amount_usd)
        try:
            json.dumps(amount, allow_nan=False)
        except ValueError as exc:
            raise ValueError("a probe cost must be a finite amount") from exc
        if amount < 0:
            raise ValueError("a probe cost cannot be negative")

    @classmethod
    def planned(cls, amount_usd: float, source: str) -> ProbeCost:
        """A ceiling kew declared before spending anything."""
        return cls(amount_usd=amount_usd, source=source, certainty=Provenance.DECLARED)

    @classmethod
    def observed(cls, amount_usd: float, source: str) -> ProbeCost:
        """Spend an execution actually reported back."""
        return cls(amount_usd=amount_usd, source=source, certainty=Provenance.MEASURED)

    @classmethod
    def unknown(cls) -> ProbeCost:
        """No cost evidence at all — never a $0.00 stand-in."""
        return cls(amount_usd=0.0, source="", certainty=Provenance.UNAVAILABLE)

    @classmethod
    def restore(
        cls,
        amount_usd: float | None,
        source: str | None,
        certainty: str | None,
    ) -> ProbeCost:
        """Rebuild a cost from stored columns; anything unreadable is unknown.

        Persistence is structural (the store may not import this module), so a
        row that predates a column, or was written by a newer build, restores
        as :meth:`unknown` rather than as a fabricated zero.
        """
        try:
            known = Provenance(str(certainty))
        except ValueError:
            return cls.unknown()
        if known is Provenance.UNAVAILABLE or amount_usd is None or source is None:
            return cls.unknown()
        try:
            return cls(amount_usd=amount_usd, source=source, certainty=known)
        except ValueError:
            return cls.unknown()


DOCTOR_SCHEMA_VERSION: Final = 1
"""The ``schema`` field of ``kew doctor --json``; bumped on a breaking change."""


class CheckStatus(StrEnum):
    """The outcome of one readiness check.

    ``warn`` and ``fail`` are both issues: either makes ``kew doctor`` exit 1.
    ``fail`` means the checked thing is broken or missing; ``warn`` means it
    works but something about it needs attention. ``skip`` is never a pass.
    """

    OK = "ok"
    WARN = "warn"
    FAIL = "fail"
    SKIP = "skip"


@dataclass(frozen=True)
class CheckResult:
    """One readiness check: a stable id, a verdict and what to do about it.

    Every text field is credential-redacted on construction, so neither
    rendering — prose or ``--json`` — can carry a host secret a probe echoed.
    ``fix`` is one runnable command or ``None``; prose belongs in ``hint``.
    ``warnings`` are advisory lines that need attention but do not change
    ``status`` (a passing check can carry one).
    """

    id: str
    status: CheckStatus
    summary: str
    hint: str | None = None
    fix: str | None = None
    details: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        def redact(text: str) -> str:
            return redact_credential_values(text, surface="readiness check")

        object.__setattr__(self, "summary", redact(self.summary))
        for name in ("hint", "fix"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, redact(value))
        for name in ("details", "warnings"):
            object.__setattr__(
                self, name, tuple(redact(line) for line in getattr(self, name))
            )

    @property
    def is_issue(self) -> bool:
        return self.status in (CheckStatus.WARN, CheckStatus.FAIL)

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "status": self.status.value,
            "summary": self.summary,
            "hint": self.hint,
            "fix": self.fix,
            "details": list(self.details),
            "warnings": list(self.warnings),
        }


def readiness_report(results: list[CheckResult]) -> dict[str, object]:
    """The ``kew doctor --json`` document for one set of results."""
    return {
        "schema": DOCTOR_SCHEMA_VERSION,
        "checks": [result.to_dict() for result in results],
        "ok": not any(result.is_issue for result in results),
    }
