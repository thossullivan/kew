"""The container-runtime registry: which names exist, and which kew can drive.

Split out of :mod:`kew.runtime_backend` for one reason, and the reason is
enforced: ``run_store`` imports ``stop_recovery``, which needs this registry to
refuse a runtime it cannot inspect — while a backend that *acts* on a container
has to reach the subprocess chokepoint, which sits above the store in the
layered-dispatch contract (``pyproject.toml``, ``lint-imports``). The registry
is pure data plus its refusal, so every layer can ask "is this runtime real,
and can this build drive it" without importing anything.

:mod:`kew.runtime_backend` re-exports all of this; import from there unless you
are below the runner layer.
"""

from __future__ import annotations

from typing import Final, Literal

#: The runtime whose container boundary is NVIDIA OpenShell (epic
#: ``openshell-runtime``), in one place. It is a config value, an audit-payload
#: value and the key of an egress-certification contract, and several modules
#: branch on it — the backend seam, the credential broker, the attestation and
#: the egress gate. A bare ``"openshell"`` literal per module is how one of
#: them silently stops matching.
OPENSHELL_RUNTIME: Final[Literal["openshell"]] = "openshell"

#: Every value ``[sandbox] runtime`` accepts. ``kew.config`` validates against
#: this, so an unknown runtime fails at load rather than at the first probe.
SUPPORTED_RUNTIMES: tuple[str, ...] = ("docker", "podman", OPENSHELL_RUNTIME)

#: The runtimes that are the ``docker`` CLI contract — one backend, one argv
#: shape, ``inspect`` / ``kill`` / ``container rm``. A narrower question than
#: "can this build drive it": the gateway runtime below is drivable and spells
#: every one of those calls differently.
DOCKER_COMPATIBLE_RUNTIMES: tuple[str, ...] = ("docker", "podman")

#: The runtimes kew can ask about a container it did not just launch — state,
#: kill, cleanup. ``openshell`` joined this set in the epic's recovery node
#: (N8): a sandbox there is reached through a gateway whose endpoint lives in
#: ``[sandbox.openshell]``, and a caller holding only a run row asks the
#: gateway named by ``OPENSHELL_GATEWAY`` or by the CLI's own default — which
#: is the endpoint a default configuration launched through too. A caller that
#: DOES hold the configuration passes it (``backend_for(..., openshell=…)``),
#: so an operator-set endpoint is still the one asked.
BACKED_RUNTIMES: tuple[str, ...] = (*DOCKER_COMPATIBLE_RUNTIMES, OPENSHELL_RUNTIME)

#: The runtimes kew can LAUNCH an agent into: epic node N6 added
#: :mod:`kew.openshell_launch`, which drives create / upload / exec / delete
#: through the gateway named in ``[sandbox.openshell]``.
#:
#: Still a separate name from :data:`BACKED_RUNTIMES`, even though the two sets
#: now hold the same members: they answer different questions — "can kew start
#: an agent here" and "can kew state a fact about a container it did not start"
#: — and a fourth runtime will land one of them before the other.
LAUNCHABLE_RUNTIMES: tuple[str, ...] = (*DOCKER_COMPATIBLE_RUNTIMES, OPENSHELL_RUNTIME)


class RuntimeBackendUnavailable(ValueError):
    """No backend implements ``runtime``, so no container fact can be stated.

    Subclasses ``ValueError`` because every site that asks a runtime about a
    container already fails closed on it: ``stop_recovery.container_state`` and
    ``fleet._container_is_stopped`` both log and answer "unknown". A runtime
    without a backend therefore degrades to unknown-with-a-reason — it must
    never read as "absent" (which frees capacity for a container that may still
    be running) and never pin capacity with nothing said.
    """

    def __init__(self, runtime: str) -> None:
        super().__init__(
            f"No container runtime backend for {runtime!r}; "
            f"this build implements {', '.join(BACKED_RUNTIMES)}."
        )
        self.runtime = runtime


def require_backed_runtime(runtime: str) -> str:
    """Return ``runtime`` when this build can state container facts about it.

    Raising is the whole point: a runtime kew cannot interrogate must surface
    as an unknown that says why, never as an unexplained "unknown" state later.
    """
    if runtime not in BACKED_RUNTIMES:
        raise RuntimeBackendUnavailable(runtime)
    return runtime


def require_launchable_runtime(runtime: str) -> str:
    """Return ``runtime`` when this build can launch an agent into it, else raise.

    The gate containment admission uses. Kept distinct from
    :func:`require_backed_runtime` because the two facts are independent: a
    runtime kew can launch into is not automatically one a later sweep holding
    only a run row can interrogate, and admission must not be refused over the
    second question.
    """
    if runtime not in LAUNCHABLE_RUNTIMES:
        raise RuntimeBackendUnavailable(runtime)
    return runtime
