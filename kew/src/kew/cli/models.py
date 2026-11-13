"""`kew models` commands: refresh the model catalog from its sources (#2711).

Thin Typer layer over ``kew.model_refresh``. ``refresh`` fetches OpenRouter
prices and model-eol lifecycle, prints the Markdown report, and writes the
catalog only with ``--write`` and only when every fetched value passed its
checks. It is an operator command; dispatch never runs it.
"""

from __future__ import annotations

import json
from http.client import HTTPException
from pathlib import Path
from typing import Annotated

import typer

from kew.cli._apps import models_app
from kew.command_outcome import COULD_NOT_RUN_EXIT_CODE
from kew.model_catalog import PACKAGED_CATALOG_PATH
from kew.model_refresh import (
    RefreshError,
    refresh_catalog,
    render_report,
    write_catalog,
)
from kew.progress import _error, _log, _success


@models_app.command("refresh")
def models_refresh(
    write: Annotated[
        bool, typer.Option("--write", help="Write the merged catalog file.")
    ] = False,
    report: Annotated[
        Path | None,
        typer.Option("--report", help="Also write the Markdown report to PATH."),
    ] = None,
    eol_feed: Annotated[
        list[str] | None,
        typer.Option("--eol-feed", help="https URL of a model-eol/0.1 feed."),
    ] = None,
    accept_large_change: Annotated[
        bool,
        typer.Option(
            "--accept-large-change",
            help="Apply a price change above 2x or below 0.5x.",
        ),
    ] = False,
    catalog: Annotated[
        Path | None,
        typer.Option("--catalog", help="Catalog file (default: the packaged one)."),
    ] = None,
) -> None:
    """Refresh catalog prices and lifecycle from OpenRouter and model-eol."""
    catalog = PACKAGED_CATALOG_PATH if catalog is None else catalog
    try:
        payload = json.loads(catalog.read_text(encoding="utf-8"))
        result = refresh_catalog(
            payload,
            eol_feeds=list(eol_feed or []),
            accept_large_change=accept_large_change,
        )
    except RefreshError as exc:
        _error(str(exc))
        raise typer.Exit(COULD_NOT_RUN_EXIT_CODE) from exc
    except (OSError, ValueError, HTTPException) as exc:
        _error(f"model catalog refresh failed: {exc}")
        raise typer.Exit(COULD_NOT_RUN_EXIT_CODE) from exc
    text = render_report(result)
    typer.echo(text)
    if report is not None:
        report.write_text(text, encoding="utf-8")
    if not result.changed:
        _success("Catalog already matches its sources")
    elif write:
        write_catalog(catalog, result.payload)
        _success(f"Wrote {catalog}; commit it with the report as the PR body")
    else:
        _log("Dry run: pass --write to update the catalog")
