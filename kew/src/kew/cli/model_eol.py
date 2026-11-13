"""`kew model-eol check` — read-only coverage of the curated lifecycle registry.

The command never fetches and never writes. ``load_config`` has already parsed
the curated registry strictly (packaged by default, or the explicit
``[model_eol].snapshot_path`` override), so a malformed document surfaces here
as an error — distinct from exit 1, which means only that a configured or
explicitly named model id has no curated row at all.
"""

from __future__ import annotations

from typing import Annotated

import typer

from kew.cli._apps import model_eol_app
from kew.config import DispatchConfig, RouteEntry, load_config
from kew.model_eol_snapshot import (
    LifecycleEntry,
    check_lifecycle,
    missing_registry_rows,
)
from kew.progress import _error, _success, _warn

MISSING_ROW_EXIT_CODE = 1


def _configured_models(config: DispatchConfig) -> list[str]:
    """Every agent, reviewer, routing, and pricing model id the config names."""
    models = [
        config.agent.model,
        config.review.model,
        *config.pricing.overrides,
    ]
    if config.routing is not None:
        for entry in (
            config.routing.default,
            config.routing.simple,
            config.routing.medium,
            config.routing.complex,
        ):
            if isinstance(entry, RouteEntry):
                models.append(entry.model)
    return models


def _load_or_exit() -> DispatchConfig:
    # An invalid registry document is a configuration error: load_config
    # raises ConfigError(kind="snapshot") with the registry reason, and the
    # root CLI boundary renders it and exits 2 (docs/cli/exit-codes.md).
    return load_config()


@model_eol_app.command("check")
def model_eol_check(
    model: Annotated[
        list[str] | None,
        typer.Option("--model", "-m", help="Extra model id to check."),
    ] = None,
) -> None:
    """Check configured and explicit model ids against the curated registry."""
    config = _load_or_exit()
    entries: dict[str, LifecycleEntry] = config.model_eol.snapshot_models
    targets = sorted(
        {
            value
            for value in [*_configured_models(config), *(model or [])]
            if isinstance(value, str) and value
        }
    )
    if not targets:
        _warn("No configured or --model model ids to check")
        raise typer.Exit(0)
    for advisory in check_lifecycle(targets, entries):
        _warn(advisory)
    missing = missing_registry_rows(targets, entries)
    if missing:
        _error(
            f"{len(missing)} of {len(targets)} model id(s) have no curated "
            "lifecycle row — update the registry"
        )
        raise typer.Exit(MISSING_ROW_EXIT_CODE)
    _success(f"Curated lifecycle registry covers all {len(targets)} model id(s)")
