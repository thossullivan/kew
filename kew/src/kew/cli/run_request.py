"""`kew run-request`: queue a kew command for the running drive (#2537).

The terminal half of the console's Run button: ``queue`` writes exactly the
request the console writes — same row, same ``run_request_queued`` audit fact,
same operator identity — and the supervised ``kew drive`` runs it. ``list`` and
``show`` read back what the drive did with it.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Annotated, Any

import typer

from kew import cli as _c
from kew.cli._apps import run_request_app
from kew.progress import _error, _success, _warn
from kew.run_requests import NotRunnable, QueuedFactFailed, RequestStateError
from kew.service import run_requests as svc
from kew.service.verbs import VerbDenied

JsonOption = Annotated[
    bool, typer.Option("--json", help="Print one JSON document instead of text.")
]


def _ledger() -> Path:
    config = _c.load_config()
    return Path(config.logging.dir).resolve() / "dispatch.db"


def _line(request: dict[str, Any]) -> str:
    detail = f" — {request['detail'].splitlines()[-1]}" if request.get("detail") else ""
    return (
        f"{request['id']}  {request['state']:<8} `{request['command']}` "
        f"by {request['operator']}{detail}"
    )


@run_request_app.command("queue")
def run_request_queue(
    command: Annotated[
        list[str], typer.Argument(help="The kew command, e.g. kew epic unhold E 12.")
    ],
    item: Annotated[
        str | None, typer.Option("--item", help="The item the command acts on.")
    ] = None,
    json_output: JsonOption = False,
) -> None:
    """Queue COMMAND for the running `kew drive`, which runs it as you.

    Only bounded commands the console offers can be queued: `kew dispatch N`,
    `kew epic continue|retry|unhold EPIC ITEM`. Exits 1 when no drive is
    running (nothing would pick it up) and 2 for a command a request cannot
    carry; nothing is written in either case."""
    text = " ".join(command)
    config = _c.load_config()
    db = Path(config.logging.dir).resolve() / "dispatch.db"
    try:
        with _c._open_run_store(str(db)) as store:
            result = svc.queue_request(
                store, command=text, item_id=item, source="cli", config=config
            )
    except (NotRunnable, VerbDenied) as exc:
        _error(f"kew run-request: {exc}.")
        raise typer.Exit(2) from None
    except svc.NoDriveRunning as exc:
        _error(f"kew run-request: {exc}.")
        raise typer.Exit(1) from None
    except (RequestStateError, QueuedFactFailed, sqlite3.Error) as exc:
        _error(f"kew run-request: the ledger could not take the request: {exc}.")
        raise typer.Exit(2) from None
    if json_output:
        typer.echo(json.dumps(result, indent=2))
        return
    request = result["request"]
    if result["created"]:
        _success(f"kew run-request: queued {request['id']} — `{request['command']}`.")
    else:
        _warn(
            f"kew run-request: `{request['command']}` is already {request['state']} "
            f"as {request['id']}; not queued twice."
        )


@run_request_app.command("list")
def run_request_list(json_output: JsonOption = False) -> None:
    """The newest run requests and what the drive did with each."""
    try:
        with _c._open_run_store(str(_ledger())) as store:
            requests = svc.list_requests(store)
            drive = svc.drive_state(store)
    except (RequestStateError, sqlite3.Error) as exc:
        _error(f"kew run-request list: {exc}.")
        raise typer.Exit(2) from None
    if json_output:
        typer.echo(json.dumps({"requests": requests, "drive": drive}, indent=2))
        return
    if not drive["running"]:
        _warn(f"kew run-request: no drive running ({drive['reason']}).")
    if not requests:
        typer.echo("no run requests.")
    for request in requests:
        typer.echo(_line(request))


@run_request_app.command("show")
def run_request_show(request_id: str, json_output: JsonOption = False) -> None:
    """One run request in full. Exits 1 when there is no such request."""
    try:
        with _c._open_run_store(str(_ledger())) as store:
            request = svc.get_request(store, request_id)
    except (RequestStateError, sqlite3.Error) as exc:
        _error(f"kew run-request show: {exc}.")
        raise typer.Exit(2) from None
    if request is None:
        _error(f"kew run-request show: no request {request_id!r}.")
        raise typer.Exit(1)
    if json_output:
        typer.echo(json.dumps(request, indent=2))
        return
    for key, value in request.items():
        if value is not None:
            typer.echo(f"{key}: {value}")
