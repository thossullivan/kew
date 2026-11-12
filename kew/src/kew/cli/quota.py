"""`kew quota` command group: release a runner lane held for quota (#2643).

A provider quota rejection holds the runner's lane until the reset the
provider gave — up to seven days for a weekly limit. The operator often knows
sooner that the wall is gone: a new agent token, a raised limit, a different
account. ``kew quota clear <runner>`` is that release. A held lane cannot
dispatch the work that would clear it, so without this command the only way
out was editing the ledger's meta table by hand.

The clearance is recorded in the ledger beside the hold — who cleared it and
when — so ``kew doctor`` and a later reader see the release, not a gap.
"""

from __future__ import annotations

import getpass
from pathlib import Path
from typing import Annotated

import typer

from kew import cli as _c
from kew.cli._apps import quota_app
from kew.progress import _error, _success
from kew.runner_quota import clear_runner_quota_hold
from kew.runners.registry import KNOWN_RUNNER_NAMES


def _operator() -> str:
    """The local account clearing the hold, for the record."""
    try:
        return getpass.getuser()
    except (KeyError, OSError):
        return "unknown"


@quota_app.command("clear")
def clear(
    runner: Annotated[
        str, typer.Argument(help="The runner lane to release, e.g. claude-code.")
    ],
) -> None:
    """Release one runner's quota hold now, before the provider's reset."""
    if runner not in KNOWN_RUNNER_NAMES:
        known = ", ".join(sorted(KNOWN_RUNNER_NAMES))
        _error(f"kew quota clear: unknown runner {runner!r} (known: {known})")
        raise typer.Exit(2)
    config = _c.load_config()
    db_path = Path(config.logging.dir).resolve() / "dispatch.db"
    if not db_path.is_file():
        typer.echo(f"No quota hold on {runner}: no ledger at {db_path}.")
        return
    with _c._open_run_store(str(db_path)) as store:
        cleared = clear_runner_quota_hold(store, runner, cleared_by=_operator())
    if cleared is None:
        typer.echo(f"No quota hold on {runner}; nothing cleared.")
        return
    _success(f"Cleared {cleared.reason()}")
    typer.echo(f"The next tick dispatches to {runner} again.")
