"""Host providers: ship a git bundle to a machine and run one job on it.

The verbs are :class:`~kew.hosts.provider.HostProvider`'s four —
``ensure_up()``, ``ship()``, ``run()``, ``release()`` — and the two providers
are :class:`~kew.hosts.local.LocalProvider` (this machine; the default, needs
no cloud account) and :class:`~kew.hosts.gcp.GcpProvider` (a Compute Engine
instance through the ``gcloud`` CLI).

The boundary both providers hold: **code in as a bundle, results out as data.**
A host never receives a GitHub, Linear, Google or model credential — the job
environment is an allowlist and credential-shaped names are refused by name
(:mod:`kew.hosts.environment`) — and what comes back is an exit code, a
duration and log text, never an instruction.

A host is built from a recipe that is itself in the repository
(:mod:`kew.hosts.provisioning`, ``provision/ubuntu-24.04.sh``), so a second
host, a sharded set of them or a CI runner is the same host, not a hand-made
one.
"""

from __future__ import annotations

import os
from pathlib import Path

from kew.config import DispatchConfig
from kew.hosts.environment import (
    AMBIENT_ALLOWLIST,
    CREDENTIAL_PREFIXES,
    CREDENTIAL_SUBSTRINGS,
    VARIABLE_NAME,
    credential_shape,
    job_environment,
    refuse_credentials,
    refuse_unusable_name,
)
from kew.hosts.errors import (
    CredentialVariableRefused,
    HostBusy,
    HostConnectionLost,
    HostError,
    HostInterrupted,
    HostJobDeadlineExceeded,
    HostNotProvisioned,
    HostTimeout,
    VariableNameRefused,
)
from kew.hosts.gcp import GcpProvider
from kew.hosts.local import LocalProvider
from kew.hosts.lock import host_lock, lock_path
from kew.hosts.provider import HostProvider, Job, JobResult
from kew.hosts.provisioning import (
    DEFAULT_RECIPE,
    PIN_FILE,
    PROVISION_TIMEOUT,
    RECIPE_DIR,
    TOOL_MARKER,
    ProvisionReport,
    RecipeHost,
    parse_tool_versions,
    pin_path,
    pinned_trivy_version,
    provision_host,
    read_pins,
    read_record,
    recipe_path,
    record_path,
    write_record,
)

__all__ = [
    "AMBIENT_ALLOWLIST",
    "CREDENTIAL_PREFIXES",
    "CREDENTIAL_SUBSTRINGS",
    "CredentialVariableRefused",
    "DEFAULT_RECIPE",
    "GcpProvider",
    "HostBusy",
    "HostConnectionLost",
    "HostError",
    "HostInterrupted",
    "HostJobDeadlineExceeded",
    "HostNotProvisioned",
    "HostProvider",
    "HostTimeout",
    "Job",
    "JobResult",
    "LocalProvider",
    "PIN_FILE",
    "PROVIDER_NAMES",
    "PROVISION_TIMEOUT",
    "RECIPE_DIR",
    "ProvisionReport",
    "RecipeHost",
    "TOOL_MARKER",
    "VARIABLE_NAME",
    "VariableNameRefused",
    "credential_shape",
    "host_lock",
    "HOST_LOCK_DIR_ENV",
    "host_lock_dir",
    "job_environment",
    "known_provider",
    "lock_path",
    "parse_tool_versions",
    "pin_path",
    "pinned_trivy_version",
    "provider_named",
    "provider_on_instance",
    "provider_pool",
    "provision_host",
    "read_pins",
    "read_record",
    "recipe_path",
    "record_path",
    "refuse_credentials",
    "refuse_unusable_name",
    "require_gcp_instance",
    "resolve_provider",
    "write_record",
]


#: Overrides where the per-host locks live. For tests and for an operator who
#: keeps kew state off the home directory; never a per-project setting.
HOST_LOCK_DIR_ENV = "KEW_HOST_LOCK_DIR"


def host_lock_dir() -> Path:
    """Where kew keeps its per-host locks: one directory per operator user.

    Keyed by the HOST, never by the project: the lock file inside is named for
    the instance, and two kew processes addressing that instance must flock the
    same inode. Under ``logging.dir`` two projects — or one project with two
    configs — locked two different files for one instance, so one's idle stop
    could stop the other's run (#2627).
    """
    override = os.environ.get(HOST_LOCK_DIR_ENV, "").strip()
    if override:
        return Path(override).expanduser().resolve()
    state = os.environ.get("XDG_STATE_HOME", "").strip() or "~/.local/state"
    return Path(state).expanduser().resolve() / "kew" / "hosts"


#: The provider names kew knows. ``HostsConfig`` refuses any other value at
#: config load, and :func:`provider_named` refuses one asked for by hand.
PROVIDER_NAMES: tuple[str, ...] = ("local", "gcp")


def provider_named(config: DispatchConfig, name: str) -> HostProvider:
    """Build the provider called *name*, whatever ``[hosts] provider`` selects.

    One place knows the names, so a command that addresses a host by hand
    (``kew hosts provision gcp``) and the configured default cannot drift
    apart. An unknown name is refused here by name rather than resolving to the
    default: silently provisioning the local machine because of a typo is
    exactly the wrong failure.

    ``gcp`` is refused at config load unless its instance is fully named, but a
    caller naming its own host (``kew verify --host gcp``, ``kew hosts provision
    gcp``) is not covered by that load-time check — ``[hosts] provider`` may
    still be ``local`` — so the same refusal is made here, by name, rather than
    at the first ``gcloud`` call.
    """
    requested = known_provider(name)
    lock_dir = host_lock_dir()
    if requested == "gcp":
        require_gcp_instance(config)
        return GcpProvider(config.hosts.gcp, lock_dir=lock_dir)
    return LocalProvider(lock_dir=lock_dir)


def known_provider(name: str) -> str:
    """*name* as a provider kew knows, or a refusal naming the two that exist."""
    requested = name.strip().lower()
    if requested not in PROVIDER_NAMES:
        raise HostNotProvisioned(f'unknown host {name!r}: expected "local" or "gcp"')
    return requested


def require_gcp_instance(config: DispatchConfig) -> None:
    """Refuse the gcp host before any ``gcloud`` call when it names no instance."""
    if not config.hosts.gcp.configured():
        raise HostNotProvisioned(
            'host "gcp" requires [hosts.gcp] project, zone and instance: '
            "without all three there is no instance to start"
        )


def provider_on_instance(
    config: DispatchConfig, *, provider: str, instance: str
) -> HostProvider:
    """The provider for ONE named instance of *provider*'s pool (#2526).

    What a placement needs and neither :func:`provider_named` nor
    :func:`provider_pool` gives it: a placement names one instance out of
    ``[hosts.gcp] instances`` — the instance it recorded on the run row — and
    the provider that runs the agent has to be bound to THAT machine, not to
    whichever one ``[hosts.gcp] instance`` happens to name. The pool builder
    takes a shard count rather than a name, so it cannot answer this.

    An empty *instance* keeps the configured default, which is what the
    single-instance case already resolves to. ``local`` ignores the name
    entirely: this machine is one host and has no pool.
    """
    requested = known_provider(provider)
    if requested != "gcp":
        return provider_named(config, requested)
    require_gcp_instance(config)
    settings = config.hosts.gcp
    named = settings.model_copy(update={"instance": instance}) if instance else settings
    return GcpProvider(named, lock_dir=host_lock_dir())


def provider_pool(
    config: DispatchConfig, *, provider: str, count: int
) -> list[HostProvider]:
    """*count* providers, one per host a sharded verification spreads across.

    The pool is ``[hosts.gcp] instances`` in its own order, so the *first*
    provider is the first instance an operator listed — the host a sharded
    verify also runs its single-run phases on. A pool shorter than the shards
    asked for is refused by name rather than doubling up: two shards on one
    instance queue behind its lock, and a verify that silently ran them one
    after the other would report the wall clock of a host doing twice the work.

    ``local`` is refused above one shard for the same reason, and it is the
    whole reason: this machine is one host, so N shards on it are N jobs in a
    queue, not N jobs at once.
    """
    if count < 1:
        raise HostNotProvisioned(
            f"a verification needs at least one shard, got {count}"
        )
    requested = known_provider(provider)
    if count == 1:
        return [provider_named(config, requested)]
    if requested == "local":
        raise HostNotProvisioned(
            f"the local host runs one job at a time, so it cannot run {count} "
            "shards at once: name a host with a pool "
            "([hosts.gcp] instances) instead"
        )
    require_gcp_instance(config)
    pool = config.hosts.gcp.pool()
    if len(pool) < count:
        raise HostNotProvisioned(
            f"host {requested!r} has {len(pool)} instance(s) configured and "
            f"{count} shards were asked for: list at least {count} in "
            "[hosts.gcp] instances"
        )
    settings = config.hosts.gcp
    lock_dir = host_lock_dir()
    return [
        GcpProvider(settings.model_copy(update={"instance": name}), lock_dir=lock_dir)
        for name in pool[:count]
    ]


def resolve_provider(
    config: DispatchConfig, *, provider: str | None = None
) -> HostProvider:
    """Build the provider ``[hosts] provider`` names, or *provider* when given.

    ``local`` is the default and the only one that needs no cloud account. The
    unknown-name and incomplete-``gcp`` refusals are :func:`provider_named`'s.
    """
    return provider_named(
        config, config.hosts.provider if provider is None else provider
    )
