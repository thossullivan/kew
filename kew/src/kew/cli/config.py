"""`kew config` commands."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer

from kew import cli as _c
from kew.cli._apps import config_app
from kew.config import ConfigError, resolved_config_path
from kew.progress import _error
from kew.service.config import (
    ReviewPolicyError,
    ReviewPolicyState,
    config_snapshot,
    human_review_state,
    valid_db_overrides,
)

# The provenance computation now lives in ``kew.service.config`` so this
# command and ``GET /api/config/effective`` answer from one place (#2298). These
# names stay as the module-private aliases the CLI (and its tests) already use.
_snapshot = config_snapshot
_valid_overrides = valid_db_overrides


def _open_effective_snapshot() -> tuple[dict[str, dict[str, Any]], Path | None, Path]:
    """Resolve config with the same DB-aware precedence used by dispatch."""
    config = _c.load_config()
    config_path = resolved_config_path()
    db_path = Path(config.logging.dir).resolve() / "dispatch.db"
    if not db_path.is_file():
        return (
            _snapshot(config, config_path=config_path, overrides={}),
            config_path,
            db_path,
        )

    with _c._open_run_store(str(db_path), read_only=True) as store:
        effective = _c.load_config(store=store)
        overrides = _valid_overrides(store)
        review = _review_state(store, effective)
    return (
        _snapshot(
            effective, config_path=config_path, overrides=overrides, review=review
        ),
        config_path,
        db_path,
    )


def _review_state(store: Any, config: Any) -> ReviewPolicyState | None:
    """The human-review policy, when this project names a delivery repository.

    Read-only on purpose: ``config effective`` opens the store read-only, and
    an expired override reverts durably on the next *write* — reporting the
    reverted values here without persisting them keeps this command honest and
    keeps the reversion evidence attached to the surface that acts on it.
    """
    try:
        return human_review_state(store, config=config)
    except ReviewPolicyError:
        return None


def _raise_effective_config_error(exc: Exception) -> NoReturn:
    """Route structured failures through the root renderer."""
    if isinstance(exc, ConfigError):
        raise exc
    _error(f"Could not resolve effective config: {exc}")
    raise typer.Exit(_c.CONFIG_ERROR_EXIT_CODE) from None


@config_app.command("effective")
def config_effective(
    sources: Annotated[
        bool,
        typer.Option(
            "--sources",
            help="Show the winning layer for every value.",
        ),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable JSON."),
    ] = False,
) -> None:
    """Show the runtime configuration and, optionally, its winning sources."""
    try:
        values, config_path, db_path = _open_effective_snapshot()
    except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
        _raise_effective_config_error(exc)

    if json_output:
        payload: dict[str, Any] = {
            field: entry["value"] for field, entry in values.items()
        }
        if sources:
            payload = {field: dict(entry) for field, entry in values.items()}
        typer.echo(json.dumps(payload, indent=2, sort_keys=True, default=str))
        return

    typer.echo("Effective configuration")
    typer.echo(f"config: {config_path or '(defaults only)'}")
    typer.echo(f"db: {db_path if db_path.is_file() else '(none)'}")
    for field, entry in values.items():
        value = json.dumps(entry["value"], sort_keys=True, default=str)
        suffix = f"  # source: {entry['source']}" if sources else ""
        typer.echo(f"{field} = {value}{suffix}")
