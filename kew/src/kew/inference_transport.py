"""Pure, fail-closed inference transport resolution."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal

from kew.switchyard import SwitchyardProbeResult

if TYPE_CHECKING:
    from kew.config import DispatchConfig
    from kew.runners.protocol import AgentRunner

TransportMode = Literal["direct", "switchyard"]
IncomingAPIFormat = Literal[
    "openai-responses", "anthropic-messages", "chat-completions"
]


@dataclass(frozen=True)
class RunnerTransportCapability:
    """A runner's proven ability to accept an isolated transport override."""

    incoming_api_formats: tuple[IncomingAPIFormat, ...] = ()
    endpoint_override: bool = False
    model_override: bool = False


@dataclass(frozen=True)
class ResolvedInferenceTransport:
    """Validated, non-secret transport values bound to one runner launch."""

    mode: TransportMode
    requested_model: str
    model: str
    config_sha256: str
    endpoint: str | None = None
    route: str | None = None
    api_format: IncomingAPIFormat | None = None
    managed: bool = False
    sidecar: bool = False
    sidecar_image: str | None = None
    sidecar_egress_network: str | None = None
    worker_credential_env_name: str | None = None
    expected_version: str = ""
    binary_sha256: str | None = None
    credential_env_names: tuple[str, ...] = ()


@dataclass(frozen=True)
class AppliedInferenceTransport:
    """Pure result of applying a resolved transport to launch inputs."""

    model: str
    argv: tuple[str, ...]
    environment: Mapping[str, str]


@dataclass(frozen=True)
class AdmittedInferenceTransport:
    """Immutable transport identity and the evidence that admitted its launch."""

    resolved: ResolvedInferenceTransport
    observed_version: str | None
    preflight_verdict: str


class TransportResolutionError(ValueError):
    """The selected runner cannot safely use the configured transport."""


class TransportAdmissionError(RuntimeError):
    """The configured transport was not healthy enough to launch a runner."""

    def __init__(self, message: str, *, reason: str | None = None) -> None:
        super().__init__(message)
        self.reason = reason


TransportPreflight = Callable[[ResolvedInferenceTransport], SwitchyardProbeResult]

_ADAPTIVE_ROUTE = "switchyard/llm_classifier"
_ADAPTIVE_REFUSAL_REASONS = frozenset(
    {
        "adaptive_admission_context_missing",
        "adaptive_routing_disabled",
        "adaptive_route_requires_switchyard",
        "adaptive_route_requires_managed_switchyard",
        "adaptive_route_requires_immutable_config",
        "adaptive_route_requires_pinned_binary",
        "adaptive_route_requires_pinned_binary_digest",
        "adaptive_route_requires_expected_version",
        "adaptive_route_requires_reviewed_credentials",
    }
)


def adaptive_route_admission_reason(
    resolved: ResolvedInferenceTransport,
    config: DispatchConfig | None,
) -> str | None:
    """Return a stable refusal code for the live classifier route.

    The ordinary passthrough admission contract remains unchanged.  A
    classifier route additionally needs an explicit experiment opt-in and a
    complete managed identity.  File checks repeat the config validator at the
    launch boundary because an operator can replace a path after loading the
    config and before dispatch starts.
    """
    if resolved.route != _ADAPTIVE_ROUTE:
        return None
    if config is None:
        return "adaptive_admission_context_missing"
    transport = config.inference_transport
    if not transport.allow_adaptive:
        return "adaptive_routing_disabled"
    if transport.mode != "switchyard":
        return "adaptive_route_requires_switchyard"
    return _adaptive_managed_identity_reason(transport.switchyard, resolved)


def _adaptive_managed_identity_reason(settings: object, resolved: object) -> str | None:
    """Check the file/version/credential half of adaptive admission."""
    sidecar = getattr(settings, "sidecar", None)
    sidecar_enabled = bool(sidecar and getattr(sidecar, "enabled", False))
    if not getattr(settings, "managed", False) and not sidecar_enabled:
        return "adaptive_route_requires_managed_switchyard"
    if not getattr(settings, "expected_version", "").strip():
        return "adaptive_route_requires_expected_version"
    config_path = getattr(settings, "config_path", None)
    if _is_immutable_file(config_path) is not True:
        return "adaptive_route_requires_immutable_config"
    if not sidecar_enabled:
        binary = getattr(settings, "binary", None)
        if _is_immutable_file(binary) is not True:
            return "adaptive_route_requires_pinned_binary"
        digest = getattr(settings, "binary_sha256", "")
        if not _pinned_binary_digest_matches(binary, digest):
            return "adaptive_route_requires_pinned_binary_digest"
    if not _reviewed_credentials_match(settings, resolved):
        return "adaptive_route_requires_reviewed_credentials"
    return None


def _is_immutable_file(path: object) -> bool | None:
    """Return whether a configured path is an absolute, non-symlink file."""
    if not isinstance(path, Path):
        return False
    try:
        return bool(path.is_absolute() and not path.is_symlink() and path.is_file())
    except OSError:
        return None


def _reviewed_credentials_match(settings: object, resolved: object) -> bool:
    """Compare declarations with the exact route's credential references."""
    config_path = getattr(settings, "config_path", None)
    route = getattr(settings, "route", "")
    declared = tuple(sorted(getattr(settings, "credential_env", ())))
    resolved_names = tuple(sorted(getattr(resolved, "credential_env_names", ())))
    if not isinstance(config_path, Path):
        return False
    try:
        from kew.switchyard_supervisor import _read_config_once, _route_credential_names

        required = _route_credential_names(_read_config_once(config_path), route)
    except (OSError, ValueError):
        return False
    return declared == resolved_names == required


def _pinned_binary_digest_matches(binary: object, digest: object) -> bool:
    """Recheck the executable's exact bytes at the admission boundary."""
    if not isinstance(binary, Path) or not isinstance(digest, str):
        return False
    if re.fullmatch(r"[0-9a-fA-F]{64}", digest) is None:
        return False
    try:
        from kew.subprocess_utils import pin_executable

        pin_executable(binary, digest)
    except (OSError, ValueError):
        return False
    return True


def require_adaptive_route_admission(
    resolved: ResolvedInferenceTransport,
    config: DispatchConfig | None,
) -> None:
    """Fail closed for an adaptive route unless its rollout gate is complete."""
    reason = adaptive_route_admission_reason(resolved, config)
    if config is not None:
        from kew.config import validate_capability_pin

        validate_capability_pin(config.inference_transport.capability)
    if reason is None:
        return
    # Every reason is an internal finite code.  Keep this assertion close to
    # the exception boundary so future changes cannot leak config/provider text.
    if reason not in _ADAPTIVE_REFUSAL_REASONS:
        reason = "adaptive_route_requires_reviewed_credentials"
    raise TransportAdmissionError(
        f"Switchyard route admission refused: {reason}", reason=reason
    )


def admit_inference_transport(
    resolved: ResolvedInferenceTransport,
    *,
    preflight: TransportPreflight | None = None,
    config: DispatchConfig | None = None,
) -> AdmittedInferenceTransport:
    """Preflight Switchyard once, while keeping direct admission free of I/O."""
    require_adaptive_route_admission(resolved, config)
    if resolved.mode == "direct":
        return AdmittedInferenceTransport(
            resolved=resolved,
            observed_version=None,
            preflight_verdict="not_required",
        )
    if resolved.sidecar:
        return AdmittedInferenceTransport(
            resolved=resolved,
            observed_version=None,
            preflight_verdict="sidecar_pending",
        )
    if resolved.managed:
        return AdmittedInferenceTransport(
            resolved=resolved,
            observed_version=None,
            preflight_verdict="managed_pending",
        )
    if preflight is None:
        raise TransportAdmissionError("Switchyard preflight is unavailable")
    result = preflight(resolved)
    if not result.admitted:
        raise TransportAdmissionError(f"Switchyard preflight refused: {result.reason}")
    if result.endpoint != resolved.endpoint or result.route != resolved.route:
        raise TransportAdmissionError("Switchyard preflight identity mismatch")
    return AdmittedInferenceTransport(
        resolved=resolved,
        observed_version=result.observed_version,
        preflight_verdict=result.reason,
    )


def _config_digest(config: DispatchConfig) -> str:
    canonical = json.dumps(
        config.inference_transport.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


def direct_route(resolved: ResolvedInferenceTransport | None) -> bool:
    """Whether the worker on this transport talks straight to its upstream.

    ``None`` is the direct route: a caller that resolved no transport has no
    switchyard anywhere in its path. Named once because the answer gates
    whether a bounded per-run key is minted at all
    (:func:`kew.credential_handoff.direct_openrouter_route`), and a caller that
    read ``egress_route`` instead — "direct" for every launch without a sidecar,
    switchyard included — attested an OpenShell launch with a per-run provider
    nothing had created.
    """
    return resolved is None or resolved.mode == "direct"


def resolve_inference_transport(
    config: DispatchConfig, runner: AgentRunner, model: str
) -> ResolvedInferenceTransport:
    """Resolve validated config for ``runner`` without I/O or mutation."""
    transport = config.inference_transport
    digest = _config_digest(config)
    if transport.mode == "direct":
        return ResolvedInferenceTransport(
            mode="direct",
            requested_model=model,
            model=model,
            config_sha256=digest,
        )

    capability = _require_transport_capability(runner)
    return _resolved_switchyard_transport(
        config,
        model=model,
        config_sha256=digest,
        capability=capability,
    )


def _require_transport_capability(runner: AgentRunner) -> RunnerTransportCapability:
    capability = getattr(runner, "transport_capability", RunnerTransportCapability())
    runner_name = getattr(runner, "name", "selected runner")
    if not capability.incoming_api_formats:
        raise TransportResolutionError(
            f"{runner_name} has no proven Switchyard incoming API capability"
        )
    if not capability.endpoint_override:
        raise TransportResolutionError(
            f"{runner_name} cannot safely receive an endpoint override"
        )
    if not capability.model_override:
        raise TransportResolutionError(
            f"{runner_name} cannot safely receive a model override"
        )
    return capability


def _resolved_switchyard_transport(
    config: DispatchConfig,
    *,
    model: str,
    config_sha256: str,
    capability: RunnerTransportCapability,
) -> ResolvedInferenceTransport:
    switchyard = config.inference_transport.switchyard
    sidecar = switchyard.sidecar
    sidecar_enabled = bool(sidecar and sidecar.enabled)
    worker_credential = {
        "anthropic-messages": "ANTHROPIC_API_KEY",
        "openai-responses": "OPENAI_API_KEY",
        "chat-completions": "OPENAI_API_KEY",
    }.get(capability.incoming_api_formats[0])
    return ResolvedInferenceTransport(
        mode="switchyard",
        requested_model=model,
        model=switchyard.route,
        config_sha256=config_sha256,
        endpoint=None if sidecar_enabled else switchyard.endpoint,
        route=switchyard.route,
        api_format=capability.incoming_api_formats[0],
        managed=switchyard.managed,
        sidecar=sidecar_enabled,
        sidecar_image=sidecar.image if sidecar_enabled and sidecar else None,
        sidecar_egress_network=(
            sidecar.egress_network if sidecar_enabled and sidecar else None
        ),
        worker_credential_env_name=(worker_credential if sidecar_enabled else None),
        expected_version=switchyard.expected_version,
        binary_sha256=switchyard.binary_sha256 or None,
        credential_env_names=switchyard.credential_env,
    )


def apply_inference_transport(
    resolved: ResolvedInferenceTransport,
    *,
    argv: list[str] | tuple[str, ...],
    env: Mapping[str, str],
    endpoint_environment_variable: str | None = None,
) -> AppliedInferenceTransport:
    """Return isolated launch values without mutating caller or process state."""
    environment = dict(env)
    if resolved.mode == "switchyard":
        if not endpoint_environment_variable:
            raise TransportResolutionError(
                "Switchyard application requires an endpoint environment variable"
            )
        assert resolved.endpoint is not None
        environment[endpoint_environment_variable] = resolved.endpoint
    return AppliedInferenceTransport(
        model=resolved.model,
        argv=tuple(argv),
        environment=MappingProxyType(environment),
    )
