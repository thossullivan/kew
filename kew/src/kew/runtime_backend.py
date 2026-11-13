"""One backend per container runtime — the seam a third runtime plugs into.

``[sandbox] runtime`` names the container boundary. Two of the three names kew
accepts, ``docker`` and ``podman``, are the same CLI contract and have always
been called the same way; ``openshell`` is a different boundary, launched
through :mod:`kew.openshell_launch` (epic ``openshell-runtime``, node N6) and
answered here by :class:`OpenShellBackend` (node N8), which spells the same
four questions as gateway calls.

The membership test lives in :mod:`kew.runtime_names` so it lives in ONE place.
Call sites used to carry their own ``{"docker", "podman"}`` literal, and each
one's else-branch answered with a clean default — capacity pinned, or a
container state reported as merely unknown — for a runtime nobody had taught it
about. :func:`backend_for` raises :class:`RuntimeBackendUnavailable` instead,
and every caller turns that into a refusal or into an unknown that says why.

Launching is NOT here: ``kew.sandbox`` still builds and runs the launch argv.
This module covers only the questions kew asks about a container that already
exists.
"""

from __future__ import annotations

import logging
import os
import shutil
from typing import TYPE_CHECKING, Protocol

from kew.runtime_names import (
    BACKED_RUNTIMES as BACKED_RUNTIMES,
)
from kew.runtime_names import (
    DOCKER_COMPATIBLE_RUNTIMES as DOCKER_COMPATIBLE_RUNTIMES,
)
from kew.runtime_names import (
    LAUNCHABLE_RUNTIMES as LAUNCHABLE_RUNTIMES,
)
from kew.runtime_names import (
    OPENSHELL_RUNTIME as OPENSHELL_RUNTIME,
)
from kew.runtime_names import (
    SUPPORTED_RUNTIMES as SUPPORTED_RUNTIMES,
)
from kew.runtime_names import (
    RuntimeBackendUnavailable as RuntimeBackendUnavailable,
)
from kew.runtime_names import (
    require_backed_runtime as require_backed_runtime,
)
from kew.runtime_names import (
    require_launchable_runtime as require_launchable_runtime,
)

if TYPE_CHECKING:
    from kew.openshell import OpenShellClient, OpenShellSettings

_logger = logging.getLogger(__name__)

#: Bounded wait for a control call on an existing container, matching the
#: timeout its caller already used.
CONTROL_TIMEOUT_SECONDS = 10


class RuntimeBackend(Protocol):
    """The questions kew asks a container boundary it did not just launch.

    ``probe`` answers "can this host use the runtime at all"; ``state`` answers
    "is this exact container still running"; ``kill`` and ``cleanup`` end it.
    The last three take the immutable container identity, never a name: names
    are reusable, so a name collision would signal the wrong container.
    """

    runtime: str

    def probe(self) -> str | None:
        """Absolute path to the runtime's control surface, or ``None``."""
        ...

    def state(self, identity: str, name: str = "") -> str:
        """``running``, ``stopped``, ``absent``, ``mismatch`` or ``unknown``."""
        ...

    def kill(self, identity: str) -> None:
        """Signal the container identified by ``identity``."""
        ...

    def cleanup(self, identity: str) -> None:
        """Force-remove the container identified by ``identity``."""
        ...


class DockerBackend:
    """``docker`` and ``podman``: one CLI contract, the calls kew already makes.

    Podman is a drop-in for every call in here, which is why one backend serves
    both and why the runtime name is carried rather than branched on.
    """

    def __init__(self, runtime: str) -> None:
        self.runtime = runtime

    def probe(self) -> str | None:
        """Resolve the runtime binary; ``None`` when this host lacks it."""
        return shutil.which(self.runtime)

    def state(self, identity: str, name: str = "") -> str:
        """Inspect one immutable id, with its recorded name as evidence."""
        from kew.stop_recovery import container_state

        return container_state(self.runtime, identity, name)

    def kill(self, identity: str) -> None:
        self._control(["kill", identity])

    def cleanup(self, identity: str) -> None:
        self._control(["container", "rm", "--force", identity])

    def _control(self, arguments: list[str]) -> None:
        from kew.subprocess_utils import run

        run([self.runtime, *arguments], check=False, timeout=CONTROL_TIMEOUT_SECONDS)


class OpenShellBackend:
    """``openshell``: the same four questions, asked through the gateway.

    The identity this backend is given is the sandbox NAME, because that is the
    identity a run row carries on this runtime — ``openshell_launch`` records
    the name and leaves ``container_id`` empty, a gateway sandbox id
    (``sbx_…``) not being the 64-hex container identity ``run_store`` validates.
    The gateway answers to the name, so the name is what is asked, and the
    sandbox the gateway reports back must carry it or nothing is stated.

    ``settings`` is ``[sandbox.openshell]`` when the caller holds the
    configuration. ``None`` means "whatever ``OPENSHELL_GATEWAY`` or the CLI's
    own default names" — the endpoint a default configuration launched through
    as well, and the only one a sweep holding just a run row can reach for.
    """

    #: Annotated ``str`` rather than left to the ``Literal`` the registry
    #: carries, so this class satisfies the :class:`RuntimeBackend` protocol —
    #: whose ``runtime`` is a mutable ``str`` attribute.
    runtime: str = OPENSHELL_RUNTIME

    def __init__(self, settings: OpenShellSettings | None = None) -> None:
        self._settings = settings

    def probe(self) -> str | None:
        """Resolve the ``openshell`` CLI; ``None`` when this host lacks it."""
        from kew.openshell import CLI

        return shutil.which(CLI)

    def state(self, identity: str, name: str = "") -> str:
        """Map ``sandbox get`` onto ``running`` / ``stopped`` / ``absent``.

        Every failure that is not an explicit "no such sandbox" reads
        ``unknown``: an unreachable gateway, a deadline, a missing CLI and a
        status word this build does not know all leave the sandbox unproven,
        and only a proven absence or termination may release a reservation.

        ``name`` is the protocol's evidence slot, and on this runtime the
        evidence is the gateway sandbox id the run row recorded. A sandbox
        answering to the name with any other id is not this run's — reusing
        its state would let one run's recovery download and delete another's
        sandbox (#2625) — so it reads ``unknown``. An empty id (a row from
        before ids were recorded, or a create not yet read back) is not
        compared.
        """
        from kew.openshell import (
            TERMINAL_STATUSES,
            OpenShellError,
            OpenShellNotFound,
        )

        if not identity.strip():
            return "unknown"
        try:
            sandbox = self._client().sandbox_get(identity)
        except OpenShellNotFound:
            return "absent"
        except (OpenShellError, ValueError) as exc:
            _logger.warning("openshell sandbox %s is unreadable: %s", identity, exc)
            return "unknown"
        if sandbox.name != identity:
            _logger.warning(
                "openshell_sandbox_identity_mismatch: asked for %s, got %s",
                identity,
                sandbox.name,
            )
            return "unknown"
        if name and sandbox.identity != name:
            _logger.warning(
                "openshell_sandbox_identity_mismatch: %s is %s, the run recorded %s",
                identity,
                sandbox.identity,
                name,
            )
            return "unknown"
        status = sandbox.status.strip().casefold()
        if status == "running":
            return "running"
        return "stopped" if status in TERMINAL_STATUSES else "unknown"

    def kill(self, identity: str) -> None:
        """Delete the sandbox: ending the workload and removing it are one call."""
        self.cleanup(identity)

    def cleanup(self, identity: str) -> None:
        """Delete the sandbox, idempotently and without raising.

        An already-absent sandbox is the outcome being asked for. Anything else
        is logged as itself: the caller's next ``state`` call is what decides
        whether the boundary is gone, so a failed delete must not be reported
        as a container fact here.
        """
        from kew.openshell import OpenShellError, OpenShellNotFound

        try:
            self._client().sandbox_delete(identity)
        except OpenShellNotFound:
            _logger.info(
                "openshell sandbox %s was already absent at cleanup; the delete "
                "it was asked for has happened",
                identity,
            )
        except (OpenShellError, ValueError) as exc:
            _logger.warning("openshell sandbox %s was not deleted: %s", identity, exc)

    def _client(self) -> OpenShellClient:
        from kew.openshell import OpenShellClient, resolve_gateway

        if self._settings is not None:
            return OpenShellClient.from_settings(self._settings)
        return OpenShellClient(
            gateway=resolve_gateway("", os.environ),
            # No version pin to compare against: a sweep holding a run row has
            # no `[sandbox.openshell] version`, and `state` runs no argv the
            # pin governs. `check_version` is the launch path's gate.
            expected_version="",
        )


def backend_for(
    runtime: str, *, openshell: OpenShellSettings | None = None
) -> RuntimeBackend:
    """Return the backend for ``runtime``, or raise naming what is missing.

    ``openshell`` is the ``[sandbox.openshell]`` block, for the callers that
    hold one; it is ignored by every other runtime, so a caller with a config
    can pass it unconditionally.
    """
    if require_backed_runtime(runtime) == OPENSHELL_RUNTIME:
        return OpenShellBackend(openshell)
    return DockerBackend(runtime)
