"""`kew hosts` command group: build a host from the checked-in recipe, drop a lease."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from kew import cli as _c
from kew.cli._apps import hosts_app
from kew.hosts import (
    DEFAULT_RECIPE,
    HostError,
    ProvisionReport,
    host_lock_dir,
    provider_named,
    provision_host,
    recipe_path,
    record_path,
)
from kew.progress import _error, _log, _success
from kew.runner_fields import exception_text

#: The command could not provision the host. There is no exit code 1 here: a
#: recipe that failed its own self-check and a host kew could not reach are
#: both "the host is not provisioned", and neither is a finding to be read.
PROVISION_FAILURE_EXIT_CODE = 2


def _report_versions(report: ProvisionReport) -> None:
    """Print the versions the host reported, in the recipe's own order."""
    for tool, version in report.versions.items():
        _log(f"{tool} {version}")


@hosts_app.command("provision")
def hosts_provision(
    name: Annotated[
        str,
        typer.Argument(
            help="Host to provision: a hosts provider name, local or gcp.",
        ),
    ],
    recipe: Annotated[
        str,
        typer.Option(
            "--recipe",
            help="Recipe shipped with kew to run (default: the Ubuntu 24.04 one).",
        ),
    ] = DEFAULT_RECIPE,
) -> None:
    """Run kew's checked-in provisioning recipe on a host and record its versions.

    The host is claimed for the whole run, brought up, built from the recipe
    kew ships (kew/src/kew/hosts/provision/) as root, and released — which arms
    its idle stop, so a provisioning run cannot leave a VM billing. The
    recipe's self-check prints one version line per tool and fails naming any
    tool it could not find; kew records those versions beside the host's lock
    only when the recipe exits zero.

    The local host is refused: it is this machine, and kew does not install
    system packages on it (see docs/hosts.md).
    """
    from kew.host_takeovers import record_lapsed_leases

    config = _c.load_config()
    record_dir = host_lock_dir()
    log_dir = Path(config.logging.dir).resolve()
    log_dir.mkdir(parents=True, exist_ok=True)
    try:
        provider = provider_named(config, name)
        # A dead holder's lease is taken over only with the takeover on the
        # audit trail before the recipe runs (#2874).
        with _c._open_run_store(str(log_dir / "dispatch.db")) as store:
            record_lapsed_leases(store, [provider], config=config)
            report = provision_host(
                provider, record_dir=record_dir, recipe=recipe_path(recipe)
            )
    except HostError as exc:
        _error(f"kew hosts provision: {exception_text(exc)}")
        raise typer.Exit(PROVISION_FAILURE_EXIT_CODE) from None
    _report_versions(report)
    if not report.ok:
        typer.echo(report.log.strip(), err=True)
        _error(
            f"{report.recipe} failed on the {report.host} host with exit "
            f"{report.exit_code}; nothing was recorded"
        )
        raise typer.Exit(PROVISION_FAILURE_EXIT_CODE)
    _success(
        f"Provisioned the {report.host} host from {report.recipe} in "
        f"{report.seconds:.1f}s; {len(report.versions)} tool versions recorded "
        f"in {record_path(record_dir, report.lock_key)}"
    )


@hosts_app.command("release")
def hosts_release(
    name: Annotated[
        str,
        typer.Argument(help="Host whose lease to drop: a hosts provider name."),
    ],
    holder: Annotated[
        str,
        typer.Option(
            "--holder",
            help="The lease token to drop, as a refused job names it.",
        ),
    ],
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Drop the lease without stopping its job: nothing is stopped.",
        ),
    ] = False,
) -> None:
    """Drop a named holder's lease on a host, by hand.

    For a lease a kew process left behind when it died: the next job is
    refused until its deadline. Only the lease naming HOLDER is dropped; any
    other is left alone. A holder that is a kew process still running on this
    machine is refused, because its job is live. So is a holder whose job
    cannot be proved stopped, unless --force drops its lease and stops nothing.
    """
    config = _c.load_config()
    try:
        provider = provider_named(config, name)
        provider.release_holder(holder, force=force)
    except HostError as exc:
        _error(f"kew hosts release: {exception_text(exc)}")
        raise typer.Exit(PROVISION_FAILURE_EXIT_CODE) from None
    if force:
        _success(
            f"Dropped the lease of {holder} on the {name} host; --force stopped "
            "nothing, so any job it left running there runs on"
        )
        return
    _success(f"Dropped the lease of {holder} on the {name} host")
