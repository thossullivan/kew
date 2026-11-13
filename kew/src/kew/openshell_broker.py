"""The OpenShell credential broker: providers instead of env credentials.

Under ``[sandbox] runtime = "openshell"`` no credential VALUE passes through
the agent's environment (epic ``openshell-runtime``, node N5). The gateway
holds the credential in a named *provider* and injects it at the TLS boundary
the policy governs; the sandbox sees the variable NAME bound to
:data:`PLACEHOLDER_CREDENTIAL_VALUE`, which is what the 2026-10-03 feasibility
spike proved both runners accept.

Four facts follow from that, and each is one function here rather than a branch
spread across admission, issuance and attestation:

**Admission asks the gateway, not the environment.** On docker a credential is
available when its variable is set; here it is available when the mapped
provider EXISTS (:func:`provider_admission`). A host that happens to export
``ANTHROPIC_API_KEY`` says nothing about whether the sandbox can authenticate,
and a provider the operator never created must refuse the dispatch before it
is paid for. Two routes need no mapped provider — the one that mints its own
key below, and a sidecar launch, whose worker credential the supervisor mints
per attempt — so WHICH route a launch takes is one answer
(:func:`broker_route`) that admission, issuance and attestation all read;
:func:`brokered_admission` is where it meets the gateway.

**A refusal says which of the three things went wrong.** No mapping, no
provider, or a gateway that could not answer are three different fixes, so
:class:`ProviderAdmission` carries the detail rather than collapsing all three
into "credential missing". A gateway that cannot be reached is a refusal WITH
its reason, never a provider silently reported as absent.

**A bounded key gets its own provider.** OpenRouter routes still carry a
bounded per-run key (``kew.run_credentials.bounded_run_credentials``) — minted
from a provisioning key, or inherited from the driver that minted it — and
:func:`run_credential_provider` puts it in ``kew-run-<run_id>`` for the life of
the run. The value reaches the gateway through the child environment, never
argv — ``kew.openshell.provider_create`` is the one place that rule lives. Only
a CONTAINED launch goes through the gateway at all, which is why brokering is a
question about a launch (:func:`brokers_launch`) and not only about config: a
run that resolved ``uncontained`` on this runtime executes on the host with the
value in its own environment.

**Attestation records the broker, not an env var.** A ledger entry naming
``ANTHROPIC_API_KEY`` on this runtime would claim a value was injected that was
not, so :func:`attested_credential_names` prefixes what it records and
:func:`broker_auth_path` gives the auth path its own ``broker:openshell:``
form.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from kew.credential_issuer import INFERENCE_ENV
from kew.runtime_names import OPENSHELL_RUNTIME

if TYPE_CHECKING:
    from kew.config import SandboxConfig
    from kew.openshell import OpenShellClient

logger = logging.getLogger(__name__)

#: The value every credential variable is bound to inside an OpenShell
#: sandbox. A runner CLI refuses to start on an EMPTY variable, so the name
#: cannot simply be dropped; the gateway supplies the real credential at the
#: endpoint, so this value authenticates nothing anywhere. It is deliberately
#: self-describing: a copy of it in a log or an issue body reads as the
#: placeholder it is rather than as a leaked token.
PLACEHOLDER_CREDENTIAL_VALUE = "kew-openshell-broker-placeholder"

#: Prefix of the per-run provider holding one bounded OpenRouter key.
RUN_PROVIDER_PREFIX = "kew-run-"

#: The upstream service a per-run OpenRouter key authenticates against.
RUN_PROVIDER_PROFILE = "openrouter"

#: Prefix of the ``auth_path`` recorded for a brokered launch. Its own form,
#: not ``env:``: ``env:NAME`` means "this launch was handed the value in NAME",
#: which is the one thing a brokered launch never is.
AUTH_PATH_PREFIX = f"broker:{OPENSHELL_RUNTIME}:"

#: Attestation prefixes. ``credential_names`` is a flat list of names shared
#: with the docker path, where a bare name means an injected value — so the two
#: kinds of name a brokered launch exposes say which they are.
PROVIDER_NAME_PREFIX = "provider:"
PLACEHOLDER_NAME_PREFIX = "placeholder:"

#: Provider names kew will carry. Narrower than the CLI's own argv check
#: (``kew.openshell._argument``) on purpose: every configured name reaches the
#: ledger inside an ``auth_path``, which ``kew.readiness._AUTH_PATH_RE``
#: validates, and a name that fails there would refuse the dispatch at profile
#: composition — long after the config that caused it was accepted.
PROVIDER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def is_brokered(config: SandboxConfig) -> bool:
    """Whether ``config`` selects the runtime where credentials are brokered."""
    return config.runtime == OPENSHELL_RUNTIME


def brokers_launch(config: SandboxConfig, *, contained: bool) -> bool:
    """Whether ONE launch's credentials are brokered by the gateway.

    :func:`is_brokered` answers a question about the configuration; this answers
    it about a launch, and the difference is the uncontained one. A dispatch
    that resolved ``uncontained`` on a host whose ``[sandbox] runtime`` happens
    to be ``openshell`` executes on that host with the credential in its own
    environment — there is no gateway anywhere in its path. Minting a provider
    for it would attest a boundary it does not have, and would hard-fail a host
    dispatch on a machine with no ``openshell`` CLI installed at all.
    """
    return contained and is_brokered(config)


def configured_provider(config: SandboxConfig, runner: str) -> str | None:
    """The provider ``[sandbox.openshell] providers`` maps ``runner`` to."""
    if not is_brokered(config):
        return None
    return config.openshell.providers.get(runner)


def run_provider_name(run_id: str) -> str:
    """The provider holding this run's own bounded key."""
    return f"{RUN_PROVIDER_PREFIX}{run_id}"


def broker_auth_path(provider: str) -> str:
    """The ``auth_path`` naming WHICH provider authenticated a launch."""
    return f"{AUTH_PATH_PREFIX}{provider}"


def launch_provider(
    config: SandboxConfig,
    runner: str,
    *,
    run_id: str,
    issued: bool,
    sidecar: bool = False,
) -> str | None:
    """The provider one launch authenticates through, or ``None`` off-broker.

    A run with its own minted key authenticates through that key's per-run
    provider; every other brokered run authenticates through the long-lived
    provider the operator mapped to the runner. ``issued`` is the REALIZED
    fact — a key in hand — so this is the form the launch sites use.

    ``sidecar`` is here rather than left to each caller because a sidecar
    launch authenticates through no provider at all (:func:`broker_route`), and
    a caller that forgot the flag would record the operator's long-lived
    credential as the thing that authenticated a launch that never touched it.
    """
    if not is_brokered(config) or sidecar:
        return None
    return run_provider_name(run_id) if issued else configured_provider(config, runner)


#: The three credential paths one brokered launch can take. Named because
#: admission, issuance and attestation each derive a different fact from the
#: route, and all three must derive it from the SAME answer: a launch admitted
#: without consulting the gateway and then attested against a provider nobody
#: created puts two incompatible claims about one launch on the chain.
SIDECAR_ROUTE = "sidecar"
MINTED_ROUTE = "minted"
MAPPED_ROUTE = "mapped"


def _holds_bounded_key() -> bool:
    """Whether this process has a bounded per-run key to put in a provider.

    Two ways to hold one, and only one of them is minting it here. A dispatch
    the graph driver launched detached was HANDED the key the driver minted
    (``kew.run_credentials.ISSUED_KEY_HASH_ENV`` marks it); the provisioning
    key that minted it stayed behind in the driver, so the child mints nothing
    and still owns a bounded key for the life of its run. Reading the
    provisioning key alone admitted such a launch against the operator's
    long-lived provider and attested that provider beside a realized route
    profile naming ``kew-run-<run_id>`` — the per-run provider
    :func:`run_credential_provider` really does create for an inherited key.
    """
    from kew.credential_issuer import has_provisioning_key
    from kew.run_credentials import ISSUED_KEY_HASH_ENV

    return ISSUED_KEY_HASH_ENV in os.environ or has_provisioning_key()


def broker_route(runner: str, *, direct: bool, sidecar: bool = False) -> str:
    """WHICH of three credential paths one brokered launch takes.

    **Sidecar** (:data:`SIDECAR_ROUTE`). The worker authenticates to the
    in-network sidecar with the per-attempt synthetic credential
    ``kew.dispatch_sandbox.prepare_sidecar_worker`` hands it out of the
    supervisor's own environment. No gateway provider holds that credential —
    the gateway governs the SIDECAR's egress, never the worker's identity — so
    a sidecar launch is not brokered at all. It resolves and attests exactly as
    it does on docker.

    **Minted** (:data:`MINTED_ROUTE`). A direct OpenRouter route whose process
    holds a bounded per-run key (:func:`_holds_bounded_key`) — minted here from
    a provisioning key, or inherited from the driver that minted it:
    ``kew.run_credentials.bounded_run_credentials`` takes either into
    :func:`run_credential_provider`, which puts it in ``kew-run-<run_id>``.
    ``direct`` is the half of that decision this module cannot see for itself —
    ``kew.credential_handoff.direct_openrouter_route`` takes it, and a
    switchyard route holds no key. Predicting a per-run provider without it
    admitted a switchyard launch on this runtime without consulting the
    gateway, minted no key, created no provider, and still attested the launch
    with a per-run provider that does not exist.

    **Mapped** (:data:`MAPPED_ROUTE`). Everything else: the long-lived provider
    the operator mapped to the runner under ``[sandbox.openshell] providers``.
    """
    from kew.sandbox import runner_credential_env_vars

    if sidecar:
        return SIDECAR_ROUTE
    declared = runner_credential_env_vars(runner)
    if direct and INFERENCE_ENV in declared and _holds_bounded_key():
        return MINTED_ROUTE
    return MAPPED_ROUTE


def attested_provider(
    config: SandboxConfig,
    runner: str,
    *,
    run_id: str | None,
    direct: bool,
    sidecar: bool = False,
) -> str | None:
    """The provider the POSTURE record names for one brokered launch.

    Posture is attested before any key is minted, so the realized ``issued``
    flag :func:`launch_provider` takes does not exist yet — this derives the
    same answer from the route. It has to be the same answer: the posture event
    and the realized route profile are two ledger facts about one launch, and a
    run whose minted key went into ``kew-run-<run_id>`` must not have the
    operator's long-lived provider attested beside it as the thing that held
    the credential. Which is why both take the route facts
    (:func:`broker_route`) and not just the ones each happens to have.
    """
    if not is_brokered(config):
        return None
    route = broker_route(runner, direct=direct, sidecar=sidecar)
    if route == SIDECAR_ROUTE:
        return None
    if route == MINTED_ROUTE and run_id is not None:
        return run_provider_name(run_id)
    return configured_provider(config, runner)


def credential_placeholders(runner: str) -> dict[str, str]:
    """Every credential NAME ``runner`` declares, bound to the placeholder."""
    from kew.sandbox import runner_credential_env_vars

    return {
        name: PLACEHOLDER_CREDENTIAL_VALUE
        for name in runner_credential_env_vars(runner)
    }


def attested_credential_names(provider: str, declared: tuple[str, ...]) -> list[str]:
    """The names-only credential exposure of one brokered launch.

    The provider that holds the credential, and the variables the sandbox sees
    holding a placeholder. Both are prefixed: an unprefixed name in this field
    means an injected value everywhere else kew writes it.
    """
    return [
        f"{PROVIDER_NAME_PREFIX}{provider}",
        *(f"{PLACEHOLDER_NAME_PREFIX}{name}" for name in declared),
    ]


@dataclass(frozen=True)
class ProviderAdmission:
    """Whether a brokered runner can authenticate, and why not when it cannot.

    ``detail`` is a sentence fragment naming the ONE thing that is wrong, so a
    missing config entry, a provider the gateway does not have, and a gateway
    that never answered are three different refusals rather than one.
    """

    provider: str | None
    available: bool
    detail: str


def provider_admission(config: SandboxConfig, runner: str) -> ProviderAdmission:
    """Ask the gateway whether ``runner``'s mapped provider is really there."""
    from kew.openshell import OpenShellClient, OpenShellError, OpenShellNotFound

    provider = configured_provider(config, runner)
    if not provider:
        return ProviderAdmission(
            None,
            False,
            f"[sandbox.openshell] providers maps no provider for {runner!r}",
        )
    try:
        found = OpenShellClient.from_settings(config.openshell).provider_get(provider)
    except OpenShellNotFound:
        return ProviderAdmission(
            provider, False, f"the OpenShell gateway has no provider {provider!r}"
        )
    except OpenShellError as exc:
        # Reported as its own cause, never as an absent provider: the operator
        # whose gateway is down must not be told to create something that is
        # already there.
        return ProviderAdmission(
            provider,
            False,
            f"the OpenShell gateway could not confirm provider {provider!r}: {exc}",
        )
    return ProviderAdmission(
        found.name, True, f"the OpenShell gateway holds provider {found.name!r}"
    )


def brokered_admission(
    config: SandboxConfig, runner: str, *, direct: bool, sidecar: bool = False
) -> ProviderAdmission | None:
    """The gateway's answer for ``runner``, or ``None`` when it has none to give.

    Asked ONCE per launch resolution, because admission and the refusal that
    explains it must describe the same gateway state: re-asking for the refusal
    text is how a provider created between the two calls produced a refusal
    whose own sentence said the gateway holds the provider it was refusing over.

    ``None`` means the gateway is not in this launch's credential path, and two
    routes are there: every launch off this runtime, and a sidecar launch
    (:func:`broker_route`), whose worker credential the supervisor mints and no
    provider holds. Consulting the gateway for a sidecar launch refused it over
    a long-lived provider it never uses, on a config that admits on docker —
    so the caller falls back to the same question docker asks.

    A route holding a bounded key of its own — minted here or inherited from
    the driver that minted it — is admitted without consulting the gateway at
    all. That key authenticates through the per-run provider
    :func:`run_credential_provider` creates for it, so requiring a mapped
    long-lived provider would refuse a provisioning-key-only config over an
    object the launch never touches — again a config that admits on docker. The
    provider is ``None`` there because its name needs a run id, which does not
    exist until the dispatch is admitted.
    """
    if not is_brokered(config):
        return None
    route = broker_route(runner, direct=direct, sidecar=sidecar)
    if route == SIDECAR_ROUTE:
        return None
    if route == MINTED_ROUTE:
        return ProviderAdmission(
            None,
            True,
            f"{runner} holds a bounded key for its own per-run provider",
        )
    return provider_admission(config, runner)


@contextmanager
def run_credential_provider(
    config: SandboxConfig, run_id: str, secret: str, *, contained: bool
) -> Iterator[str | None]:
    """Hold one bounded key in ``kew-run-<run_id>`` for the life of the run.

    The key may have been minted by this process or inherited from the driver
    that minted it (``kew.run_credentials.bounded_run_credentials`` wraps both
    in this); either way it is this run's key, and the provider holding it is
    this run's.

    Yields ``None`` for every launch this runtime does not broker — another
    runtime, or an uncontained run on this one (:func:`brokers_launch`) — so the
    issuance path wraps every route in this and branches nowhere. The provider
    is deleted on EVERY exit: the minting caller's own ``finally`` revokes the
    key, and nesting this inside it is what makes the provider gone before the
    key it holds is revoked.
    """
    if not brokers_launch(config, contained=contained):
        yield None
        return
    name = run_provider_name(run_id)
    client = _run_provider_client(config, secret)
    _create_run_provider(client, name)
    try:
        yield name
    finally:
        _delete_run_provider(client, name)


def _create_run_provider(client: OpenShellClient, name: str) -> None:
    """Create the per-run provider, cleaning up a create of unknown outcome.

    :class:`~kew.openshell.OpenShellOutcomeUnknown` documents the one thing a
    caller must not read from it: that nothing happened. A create that broke
    after it went on the wire may already have committed, so letting it out
    unhandled would leave a provider holding a key the caller revokes moments
    later — a live provider injecting a dead credential, which reaches the
    operator as an inference error with no cause attached. The delete runs
    first and the original failure is still raised; a provider that was in fact
    never created answers ``NotFound``, which the cleanup already treats as
    done. A failure that never reached the gateway
    (:class:`~kew.openshell.OpenShellGatewayUnreachable`) is left alone: there
    is nothing on the gateway to delete.
    """
    from kew.openshell import OpenShellOutcomeUnknown

    try:
        client.provider_create(
            name, profile=RUN_PROVIDER_PROFILE, env_names=(INFERENCE_ENV,)
        )
    except OpenShellOutcomeUnknown:
        _delete_run_provider(client, name)
        raise


def _run_provider_client(config: SandboxConfig, secret: str) -> OpenShellClient:
    """A client whose environment carries the minted value — never argv.

    ``kew.openshell.provider_create`` forwards the value by NAME out of the
    environment it is given. The minted key was never in ``os.environ``, so the
    one place it is bound to its name is here, in a mapping that lives for the
    duration of that single call.
    """
    from kew.openshell import OpenShellClient

    return OpenShellClient.from_settings(
        config.openshell, {**os.environ, INFERENCE_ENV: secret}
    )


def _delete_run_provider(client: OpenShellClient, name: str) -> None:
    """Delete the per-run provider, loudly, without masking the run's failure.

    Raising here would replace whatever exception is unwinding — including the
    dispatch failure the operator actually needs — and would skip the key
    revocation this sits inside. So the failure is logged as itself: a provider
    left behind holds a key that is revoked moments later, which authenticates
    nothing, while a lost dispatch cause is unrecoverable.
    """
    from kew.openshell import OpenShellError, OpenShellNotFound

    try:
        client.provider_delete(name)
    except OpenShellNotFound:
        logger.warning("openshell provider %s was already gone at cleanup", name)
    except OpenShellError as exc:
        logger.error(
            "openshell provider %s could not be deleted (%s); its key is "
            "revoked next and authenticates nothing, but the provider remains "
            "on the gateway",
            name,
            exc,
        )
