"""Immutable route identity for an explicitly identified detached child."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

from kew.credential_issuer import INFERENCE_ENV, IssuerRefused

if TYPE_CHECKING:
    from kew.inference_transport import ResolvedInferenceTransport
    from kew.runners.protocol import AgentRunner


CLAUDE_MODE_CREDENTIAL_NAMES: dict[str, frozenset[str]] = {
    "anthropic-api": frozenset(
        {
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "CLAUDE_CODE_OAUTH_TOKEN",
            "ANTHROPIC_BASE_URL",
        }
    ),
    "bedrock": frozenset(
        {
            "CLAUDE_CODE_USE_BEDROCK",
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "AWS_SESSION_TOKEN",
            "AWS_REGION",
            "AWS_DEFAULT_REGION",
            "AWS_PROFILE",
            "AWS_BEARER_TOKEN_BEDROCK",
            "ANTHROPIC_BEDROCK_BASE_URL",
        }
    ),
    "vertex": frozenset(
        {
            "CLAUDE_CODE_USE_VERTEX",
            "GOOGLE_APPLICATION_CREDENTIALS",
            "CLOUD_ML_REGION",
            "ANTHROPIC_VERTEX_PROJECT_ID",
            "GOOGLE_CLOUD_PROJECT",
            "ANTHROPIC_VERTEX_BASE_URL",
        }
    ),
}
CLAUDE_CREDENTIAL_NAMES = frozenset().union(*CLAUDE_MODE_CREDENTIAL_NAMES.values())


def _claude_auth_mode() -> str:
    """Resolve documented boolean selectors; refuse conflicting provider modes."""
    modes = [
        mode
        for mode in ("bedrock", "vertex")
        if os.environ.get(f"CLAUDE_CODE_USE_{mode.upper()}", "").lower()
        in {"1", "true"}
    ]
    if len(modes) > 1:
        raise CredentialRouteChanged()
    return modes[0] if modes else "anthropic-api"


@dataclass(frozen=True)
class DriverCredentialRoute:
    runner: str
    model: str
    transport: str
    sidecar: bool
    bounded_key: bool
    required_credential_names: frozenset[str]
    claude_auth_mode: str | None = None

    def evidence(self) -> dict[str, object]:
        return {
            **asdict(self),
            "required_credential_names": sorted(self.required_credential_names),
        }

    @property
    def digest(self) -> str:
        canonical = json.dumps(self.evidence(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()


def direct_openrouter_route(
    model: str, credential_names: tuple[str, ...], *, direct: bool
) -> bool:
    """Use bounded keys only when the worker talks directly to OpenRouter."""
    return direct and (
        model.startswith("openrouter/") or INFERENCE_ENV in credential_names
    )


def credential_route(
    runner: AgentRunner, transport: ResolvedInferenceTransport
) -> DriverCredentialRoute:
    """Bind the same resolved values that determine the worker's credentials."""
    bounded = direct_openrouter_route(
        transport.model,
        tuple(getattr(runner, "required_credentials", ())),
        direct=transport.mode == "direct",
    )
    mode = "sidecar" if transport.sidecar else transport.mode
    if mode == "direct" and not bounded and runner.name in {"claude-code", "codex"}:
        mode = "native"
    claude_mode = (
        _claude_auth_mode()
        if mode == "native" and runner.name == "claude-code"
        else None
    )
    from kew.credential_issuer import MANAGEMENT_CREDENTIAL_ENVS
    from kew.subprocess_utils import PUBLICATION_CREDENTIAL_ENVS

    required = (
        (
            CLAUDE_MODE_CREDENTIAL_NAMES[claude_mode]
            if claude_mode is not None
            else _required_credential_names(runner, transport, bounded=bounded)
        )
        - set(MANAGEMENT_CREDENTIAL_ENVS)
        - set(PUBLICATION_CREDENTIAL_ENVS)
    )
    return DriverCredentialRoute(
        runner.name,
        transport.model,
        mode,
        transport.sidecar,
        bounded,
        required,
        claude_mode,
    )


def _required_credential_names(
    runner: AgentRunner, transport: ResolvedInferenceTransport, *, bounded: bool
) -> frozenset[str]:
    """Keep only authentication names consumed by the resolved route."""
    if transport.mode == "switchyard":
        return frozenset(transport.credential_env_names)
    if bounded:
        return frozenset({INFERENCE_ENV})
    if runner.name == "codex":
        return frozenset()
    return frozenset(getattr(runner, "required_credentials", ()))


@dataclass(frozen=True)
class DetachedCredentialHandoff:
    digest: str | None
    run_id: str | None


_HANDOFF: ContextVar[DetachedCredentialHandoff | None] = ContextVar(
    "detached_credential_handoff", default=None
)


@contextmanager
def detached_credential_handoff(
    digest: str | None, run_id: str | None
) -> Iterator[None]:
    """Scope explicit CLI handoff flags to this dispatch, including its launch."""
    handoff = (
        DetachedCredentialHandoff(digest, run_id)
        if digest is not None or run_id is not None
        else None
    )
    token = _HANDOFF.set(handoff)
    try:
        yield
    finally:
        _HANDOFF.reset(token)


def is_detached_credential_child() -> bool:
    return _HANDOFF.get() is not None


class CredentialRouteChanged(IssuerRefused):
    reason = "credential route changed"


def require_credential_route(
    runner: AgentRunner, transport: ResolvedInferenceTransport
) -> DriverCredentialRoute:
    """Refuse route drift at each resolution before the worker launches."""
    decision = credential_route(runner, transport)
    handoff = _HANDOFF.get()
    if (
        handoff is not None
        and handoff.digest is not None
        and handoff.digest != decision.digest
    ):
        raise CredentialRouteChanged()
    return decision
