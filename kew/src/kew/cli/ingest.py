"""`kew ingest` command group: ingest external runner usage (#256)."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

if TYPE_CHECKING:
    pass

from kew import cli as _c
from kew.cli._apps import ingest_app
from kew.progress import _error, _success


@ingest_app.command("file")
def ingest_file(
    path: Annotated[Path, typer.Argument(help="OTLP/JSON metrics export to ingest")],
) -> None:
    """Ingest a Claude Code OTLP/JSON metrics export into the run store."""
    import json

    from kew.ingest.core import ingest_document

    if not path.exists():
        _error(f"kew ingest file: no such file: {path}")
        raise typer.Exit(2)
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _error(f"kew ingest file: could not read JSON: {exc}")
        raise typer.Exit(2) from exc

    config = _c.load_config()
    db_path = Path(config.logging.dir).resolve() / "dispatch.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with _c._open_run_store(str(db_path)) as store:
            summary = ingest_document(
                doc, ingest_source=f"file:{path.name}", store=store
            )
    except Exception as exc:  # noqa: BLE001 — surface like `kew ingest serve`, not a traceback
        _error(f"kew ingest file: could not ingest metrics: {exc}")
        raise typer.Exit(2) from exc
    _success(
        f"ingested {summary.ingested}, deduped {summary.deduped}, "
        f"skipped {summary.skipped} data point(s)"
    )


@ingest_app.command("serve")
def ingest_serve() -> None:
    """Run a loopback OTLP/HTTP receiver for Claude Code metrics.

    Point Claude Code at it:
      CLAUDE_CODE_ENABLE_TELEMETRY=1 OTEL_METRICS_EXPORTER=otlp
      OTEL_EXPORTER_OTLP_PROTOCOL=http/json
      OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:<port>
    Ctrl-C to stop.
    """
    from kew.ingest.server import make_server

    config = _c.load_config()
    if not config.ingest.enabled:
        _error("kew ingest serve: set [ingest].enabled = true to run the receiver")
        raise typer.Exit(2)
    db_path = Path(config.logging.dir).resolve() / "dispatch.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    host, port = config.ingest.host, config.ingest.port
    with _c._open_run_store(str(db_path)) as store:
        server = make_server(host, port, store)
        _success(f"kew ingest serve: listening on http://{host}:{port}{'/v1/metrics'}")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            _success("kew ingest serve: stopped")
        finally:
            server.server_close()
