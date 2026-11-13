"""Resolving the exact leg a launch would cross. Spends nothing, ever (#2302).

Split out of :mod:`kew.readiness_probe` so that a read-only surface can ask
*what would run* without being able to reach the code that can *charge for it*.
``tests/test_readiness_probe.py`` keeps the spending module behind exactly one
door (``kew doctor``); the dispatch launchpad needs this resolution and may not
open that door, so the resolution lives here and the probe module imports it.

Nothing in this file calls a provider, takes a lease, writes a row or spends a
cent. It reads configuration, resolves the runner and the transport exactly as
dispatch does, and observes the launch boundary — the executable digest, or the
immutable image ID behind a mutable tag, which is why a contained route fails
closed here rather than being described as contained on a tag alone. The
resulting fingerprint is what probe evidence is filed and looked up under.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from kew import subprocess_utils
from kew.config import DispatchConfig
from kew.inference_transport import ResolvedInferenceTransport
from kew.readiness import (
    Containment,
    RealizedLaunchBoundary,
    RealizedRouteProfile,
    compose_realized_profile,
    compose_route_profile,
)
from kew.runners.protocol import AgentRunner
from kew.sandbox import SandboxDecision
from kew.sandbox_caps import ResourceCaps

if TYPE_CHECKING:
    from collections.abc import Mapping

    from kew.run_store import SqliteRunStore
    from kew.runners.registry import ResolvedRoute

PROBE_PROMPT: Final = (
    "Reply with exactly OK and nothing else. "
    "Do not use any tool, do not read or write any file, do not explain."
)
"""The whole task. Anything a turn could *do* is absent by construction.

Defined here rather than in :mod:`kew.readiness_probe` so the prompt catalog
(:mod:`kew.prompt_previews`) can show it without importing the spending module.
"""

PROBE_RUN_ID: Final = "readiness-probe"
"""Identity-free placeholder run id used only to compose a fingerprint.

:meth:`RealizedRouteProfile.material_json` drops run/attempt identity before
hashing, so this never reaches the fingerprint — it exists because composing a
profile requires *a* run id, and a probe has no customer run to borrow one
from.
"""


# Unbound previews cannot reuse durable evidence. Production lookups supply a ledger.
_PREVIEW_PROXY_KEY = os.urandom(32).hex()


def _inherited_proxy_digest(
    names: Iterable[str], store: SqliteRunStore | None, *, create_key: bool
) -> str:
    """Snapshot inherited values with the non-exported operator HMAC key.

    Without a key (none created yet, and this caller may not create one) the
    digest is keyed like an unbound preview: it matches nothing durable, so
    the posture reads as not verified rather than failing the read (#2564).
    """
    pairs = sorted((name, os.environ.get(name, "")) for name in names)
    if not pairs:
        return ""
    key = store.readiness_proxy_key(create=create_key) if store is not None else None
    material = json.dumps(pairs, ensure_ascii=True, separators=(",", ":"))
    return hmac.new(
        (key or _PREVIEW_PROXY_KEY).encode(), material.encode(), hashlib.sha256
    ).hexdigest()


@dataclass(frozen=True)
class ProbeRoute:
    """The exact production leg one live confirm would exercise.

    Resolving this costs nothing: it reads config, resolves the runner and
    transport exactly as dispatch does, and observes the launch boundary
    (executable digest or immutable image ID). The resulting fingerprint is
    what probe evidence is filed and looked up under.
    """

    runner: AgentRunner
    model: str
    profile: RealizedRouteProfile
    sandbox: SandboxDecision
    network: str
    transport: ResolvedInferenceTransport | None = None
    proxy_environment: str = "none"
    #: The effective container caps the probe launches under — copied from
    #: config exactly as ``network`` and ``proxy_environment`` are, so the
    #: probe exercises the operator's caps and not the ceilings (#2360).
    caps: ResourceCaps | None = None
    inherited_proxy_names: tuple[str, ...] = ()
    blanked_proxy_names: tuple[str, ...] = ()
    inherited_proxy_digest: str = ""

    @property
    def runner_name(self) -> str:
        return self.profile.runner

    @property
    def fingerprint(self) -> str:
        """The realized route profile's fingerprint, and only that.

        A probe's identity has to be something its consumer can reconstruct:
        ``service/work.py`` looks readiness up by ``profile.fingerprint`` from
        the recorded profile alone. Mixing this launch's proxy posture in made
        every settled probe unfindable and silently retired the
        ``readiness_refuted`` gate (governed review round 4, 2026-09-21).
        ``proxy_material`` below reports the posture this route resolved
        under, for a caller that wants it. Nothing records it yet, so a probe
        confirmed under one posture is reused under another: binding the
        posture to a probe needs the stored probe to carry it, which is a
        schema step (kew issue #1973).
        """
        return self.profile.fingerprint

    @property
    def proxy_material(self) -> dict[str, object]:
        """The proxy posture this probe ran under, names and a digest only."""
        return {
            "proxy_environment": self.proxy_environment,
            "proxy_variables": {
                "inherited": list(self.inherited_proxy_names),
                "blanked": list(self.blanked_proxy_names),
                "values_digest": self.inherited_proxy_digest,
            },
        }

    @property
    def image(self) -> str | None:
        return self.sandbox.image


def _probe_auth_path(runner_name: str, transport: ResolvedInferenceTransport) -> str:
    """WHICH credential the probe would authenticate with — never its value."""
    from kew.sandbox import runner_credential_env_vars

    if transport.sidecar:
        return "synthetic:per-attempt"
    names = transport.credential_env_names or runner_credential_env_vars(runner_name)
    present = next((name for name in names if os.environ.get(name)), None)
    return f"env:{present}" if present is not None else "cli-login"


def _probe_boundary(
    config: DispatchConfig,
    *,
    runner_name: str,
    model: str,
    transport: ResolvedInferenceTransport,
    decision: SandboxDecision,
) -> RealizedLaunchBoundary:
    """Resolve the boundary the probe launches across, fail-closed.

    ``source_provider`` stays ``None`` on purpose: a probe has no work item, so
    it has no source identity to claim. That is also why probe evidence can
    never be confused with a customer run's profile — the tracker column of a
    probe boundary is structurally empty.
    """
    from kew.orchestrator import _requested_service_tier
    from kew.pricing import infer_provider
    from kew.sandbox import image_id_runtime, require_image_id

    shared: dict[str, str | None] = {
        "inference_provider": infer_provider(model, runner_name) or None,
        "endpoint": transport.endpoint,
        "service_tier": _requested_service_tier(config, runner_name),
        "auth_path": _probe_auth_path(runner_name, transport),
    }
    if decision.image is None:
        return _host_probe_boundary(runner_name, decision, shared)
    image_id = require_image_id(
        # Through the image seam: on ``openshell`` the probe would otherwise
        # ask a gateway CLI with no image surface, and refuse a boundary that
        # is in fact ready (``kew.sandbox.image_id_runtime``).
        image_id_runtime(config.sandbox, decision.runtime),
        decision.image,
        runner=runner_name,
        refusal="the readiness probe is refused before any charge",
    )
    return RealizedLaunchBoundary(
        containment=Containment.CONTAINED,
        image_ref=decision.image,
        image_id=image_id,
        **shared,
    )


def _host_probe_boundary(
    runner_name: str, decision: SandboxDecision, shared: Mapping[str, str | None]
) -> RealizedLaunchBoundary:
    """A host-side probe boundary, honest about the posture it resolved."""
    native = subprocess_utils.observe_runner_executable(runner_name)
    posture = {
        "native": Containment.NATIVE,
        "uncontained": Containment.UNCONTAINED,
    }.get(decision.posture, Containment.HOST)
    return RealizedLaunchBoundary(
        containment=posture,
        runner_executable=None if native is None else native.path,
        runner_version=None if native is None else native.version,
        runner_digest=None if native is None else native.sha256,
        **shared,
    )


def resolve_probe_route(
    config: DispatchConfig,
    *,
    complexity: str | None = None,
    store: SqliteRunStore | None = None,
    resolved: ResolvedRoute | None = None,
    create_key: bool = False,
) -> ProbeRoute:
    """Resolve the exact leg a live confirm would cross. Spends nothing.

    ``resolved`` is for a caller that has already resolved its own route and
    needs THAT leg described — the review lane, whose runner comes from the
    ``[review]`` pin rather than from node routing (#2302). Left ``None`` (every
    probe path) the route is resolved here from ``complexity``, exactly as
    dispatch resolves it.

    ``store`` is the ledger the OpenShell egress gate reads its certification
    from, and the one the inherited-proxy digest is keyed with. Without it an
    OpenShell boundary is refused as unproven, because "no ledger to check" is
    not a proof (#2319).

    ``create_key`` is for the spend path alone (the live confirm): only it may
    create the readiness HMAC key when absent. Every passive read — the
    dispatch plan, ``kew doctor``'s checks, any GET route — leaves it
    ``False`` and never writes a secret to disk as a side effect (#2564).
    """
    from kew.inference_transport import resolve_inference_transport
    from kew.runners.registry import resolve_runner
    from kew.sandbox import (
        _proxy_variable_actions,
        resolve_transport_sandbox,
        resource_caps,
    )

    route = resolved if resolved is not None else resolve_runner(config, complexity)
    runner_name = route.runner.name
    transport = resolve_inference_transport(config, route.runner, route.model)
    # Through the one helper dispatch resolves with, handed the same ledger the
    # OpenShell egress gate reads its proof from. A probe is not a preview: it
    # launches across the boundary and spends, so resolving it storeless
    # refused every OpenShell boundary as uncertified with a fresh matching
    # certification on that exact store (#2389 review).
    decision = resolve_transport_sandbox(
        config.sandbox,
        runner=runner_name,
        inference_transport=transport,
        store=store,
        # The probe's own contained launch is a container command line
        # (`_contained_launch` -> `build_run_argv`), not a gateway handover,
        # so a gateway runtime is refused here rather than argv-ified into
        # a CLI that has no `run` subcommand. A refusal out of this call is
        # already how the probe reports an unresolvable boundary. (A sidecar
        # route ignores this flag: it is refused on that runtime for its
        # topology.)
        gateway_launch=False,
    )
    profile = compose_realized_profile(
        run_id=PROBE_RUN_ID,
        attempt_number=1,
        route=compose_route_profile(
            runner=runner_name, requested_model=route.model, transport=transport
        ),
        boundary=_probe_boundary(
            config,
            runner_name=runner_name,
            model=route.model,
            transport=transport,
            decision=decision,
        ),
    )
    proxy_actions = _proxy_variable_actions(config.sandbox.proxy_environment)
    return ProbeRoute(
        runner=route.runner,
        model=route.model,
        profile=profile,
        sandbox=decision,
        network=config.sandbox.network,
        transport=transport,
        proxy_environment=config.sandbox.proxy_environment,
        caps=resource_caps(config.sandbox),
        inherited_proxy_names=tuple(proxy_actions["inherited"]),
        blanked_proxy_names=tuple(proxy_actions["blanked"]),
        inherited_proxy_digest=_inherited_proxy_digest(
            proxy_actions["inherited"], store, create_key=create_key
        ),
    )


__all__ = ["PROBE_RUN_ID", "ProbeRoute", "resolve_probe_route"]
