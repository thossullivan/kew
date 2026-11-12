"""`kew skills` — provision and maintain shipped governance artifacts.

Artifacts install from the wheel's package data only (no remote fetch). The
security-guard hook is defense-in-depth: it catches destructive shell
patterns and protects .claude/ from agent writes; isolation comes from
sandboxed dispatch, not this hook.
"""

import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from kew.config import DispatchConfig, load_config
from kew.skills_catalog import (
    Artifact,
    LocallyModifiedError,
    apply_artifact,
    artifact_state,
    diff_artifact,
    load_manifest,
)

skills_app = typer.Typer(
    help="Install and maintain kew's shipped agent skills and hooks."
)
_console = Console()


def _select(names: list[str], install_all: bool) -> list[Artifact]:
    manifest = load_manifest()
    if install_all or not names:
        return manifest
    by_name = {a.name: a for a in manifest}
    unknown = [n for n in names if n not in by_name]
    if unknown:
        _console.print(f"[red]Unknown artifact(s): {', '.join(unknown)}[/red]")
        _console.print(f"Available: {', '.join(sorted(by_name))}")
        raise typer.Exit(2)
    return [by_name[n] for n in names]


#: The ``schema`` field of ``kew skills list --json``; bumped on a breaking change.
SKILLS_LIST_SCHEMA_VERSION = 1


@skills_app.command("list")
def list_(
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print one JSON document instead of a table."),
    ] = False,
) -> None:
    """Show shipped artifacts and their installed state."""
    config = load_config()
    if json_output:
        typer.echo(json.dumps(_list_document(config), indent=2))
        return
    table = Table(title="kew shipped artifacts")
    table.add_column("name")
    table.add_column("kind")
    table.add_column("version")
    table.add_column("state")

    for a in load_manifest():
        state = artifact_state(a, Path.cwd(), config)
        style = {
            "current": "green",
            "outdated": "yellow",
            "locally-modified": "red",
            "not-installed": "dim",
        }.get(state, "")
        cell = f"[{style}]{state}[/{style}]" if style else state
        table.add_row(a.name, a.kind, str(a.version), cell)
    _console.print(table)


def _list_document(config: DispatchConfig) -> dict[str, object]:
    """The ``kew skills list --json`` document: the rows the table prints.

    ``drift`` is True only for a locally modified copy — the drift
    ``kew skills diff`` shows; an ``outdated`` copy is kew's own older
    version, which ``kew skills update`` replaces without ``--force``."""
    skills = []
    for a in load_manifest():
        state = artifact_state(a, Path.cwd(), config)
        skills.append(
            {
                "name": a.name,
                "kind": a.kind,
                "version": a.version,
                "target": a.target,
                "state": state,
                "installed": state != "not-installed",
                "drift": state == "locally-modified",
            }
        )
    return {"schema": SKILLS_LIST_SCHEMA_VERSION, "skills": skills}


def _apply_many(names: list[str], install_all: bool, force: bool) -> None:
    config = load_config()
    had_error = False
    for a in _select(names, install_all):
        try:
            result = apply_artifact(a, Path.cwd(), config, force=force)
        except LocallyModifiedError as exc:
            _console.print(f"[red]{exc}[/red]")
            had_error = True
            continue
        _console.print(f"{a.name}: {result}")
    if had_error:
        raise typer.Exit(1)


@skills_app.command()
def install(
    names: Annotated[
        list[str] | None,
        typer.Argument(help="Artifact names (see `kew skills list`)."),
    ] = None,
    install_all: Annotated[
        bool, typer.Option("--all", help="Install every shipped artifact.")
    ] = False,
    force: Annotated[
        bool, typer.Option("--force", help="Overwrite locally modified copies.")
    ] = False,
) -> None:
    """Install shipped artifacts into the repo's .claude/ directory."""
    if not install_all and not names:
        _console.print("Nothing selected. Pass artifact names or --all.")
        raise typer.Exit(2)
    _apply_many(list(names or []), install_all, force)


@skills_app.command()
def update(
    names: Annotated[
        list[str] | None,
        typer.Argument(
            help=(
                "Artifact names; default: converge all "
                "(refresh outdated, install missing)."
            )
        ),
    ] = None,
    force: Annotated[
        bool, typer.Option("--force", help="Overwrite locally modified copies.")
    ] = False,
) -> None:
    """Refresh installed artifacts to the shipped versions."""
    _apply_many(list(names or []), install_all=not names, force=force)


@skills_app.command()
def diff(
    names: Annotated[
        list[str] | None,
        typer.Argument(help="Artifact names; default: all."),
    ] = None,
) -> None:
    """Show drift between installed copies and shipped versions."""
    config = load_config()
    found_drift = False
    for a in _select(list(names or []), install_all=not names):
        d = diff_artifact(a, Path.cwd(), config)
        if d:
            _console.print(d, highlight=False)
            found_drift = True
    if found_drift:
        raise typer.Exit(1)
