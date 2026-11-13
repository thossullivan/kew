"""`kew serve` command group: the localhost web UI + API + approval inbox.

``kew serve`` binds the FastAPI app (UI + API + approval inbox) on a loopback
host, refusing an unsafe non-loopback/wildcard bind unless ``--expose`` names a
concrete address (the browser Host/Origin allowlist needs it), and refusing to
expose at all until ``[flight_recorder].trusted_proxies`` declares the TLS
terminator in front (kew itself speaks plaintext HTTP). ``kew serve url``
prints the tokened access URL of the running instance.
"""

from __future__ import annotations

import contextlib
import json as _json
import logging
import sys
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer

from kew import cli as _c
from kew.audit import DEFAULT_ANCHORS_PATH
from kew.cli._apps import serve_app
from kew.config import DispatchConfig, _is_loopback
from kew.config import project_root as _project_root
from kew.ledger_identity import canonical_project_root, open_managed_store
from kew.links import UPGRADE_HINT
from kew.policy import repo_root as _policy_repo_root
from kew.progress import _error, _warn
from kew.run_store import SchemaTooNewError, SqliteRunStore

if TYPE_CHECKING:
    from kew.outcomes import PRReadings
    from kew.serve.sweep import SweepCycleResult

_logger = logging.getLogger(__name__)


def _now_iso() -> str:
    """This instant, ISO-8601 UTC — the sweep's heartbeat stamp."""
    return datetime.now(UTC).isoformat()


def refresh_driver_locks(store: Any, config: DispatchConfig) -> int:
    """Snapshot every known epic's driver-lock facts into the ledger (#2297).

    The lock file holds the two facts a heartbeat age cannot supply — who holds
    the epic and whether that process is still alive — and the heartbeat that
    keeps beating mid-tick. No serve-side reading can open it: ``kew.serve`` and
    ``kew.service`` are both forbidden from importing ``kew.graph``, and the
    serve process need not even run as a user who could. So the sweep, which is
    CLI-side, looks once a cycle and records what it saw (with the instant it
    looked, so an expired snapshot can be refused) for
    ``kew.service.epics.driver_liveness`` to pick up. Without it the console
    thresholded the tick stamps alone and called a driver that was provably alive
    mid-long-tick "gone" while ``kew epic status`` called it live.

    A FREE lock is recorded as the absence it is (all-None facts, dated): no
    driver holds the epic right now, which between ticks is the normal state and
    is evidence of nothing either way — recorded, not skipped, so a previous
    observation cannot linger as if it were current. An epic with NO driver-state
    row and no holder is left alone rather than seeded with an empty one: that row
    is what the Epics list reads its last tick off, and an all-NULL one would hide
    the journal's own stamp behind "never ticked". Returns how many epics were
    observed; a lock that cannot be read is skipped, never guessed at.
    """
    observed_at = _now_iso()
    driven = store.list_epic_driver_states()
    looked = 0
    for epic_ref in store.list_epic_refs():
        try:
            holder = _c.lock_holder(_c._epic_lock_path(config, epic_ref))
        except OSError:
            continue
        if holder is None and epic_ref not in driven:
            continue
        store.record_epic_driver_lock(
            epic_ref,
            pid=getattr(holder, "pid", None),
            pid_alive=None if holder is None else holder.is_alive(),
            heartbeat_at=(
                holder.heartbeat.isoformat()
                if holder is not None and holder.heartbeat is not None
                else None
            ),
            observed_at=observed_at,
        )
        looked += 1
    return looked


def sweep_cycle(
    store_factory: Callable[[], AbstractContextManager[Any]],
    config: DispatchConfig,
    *,
    health_reader: Any | None = None,
    readings: PRReadings | None = None,
) -> SweepCycleResult:
    """One serve sweep cycle: outcome sync, orphan reconciliation, the driver-lock
    snapshot and the free GitHub rate-limit reading — then REPORT what it saw.

    Fail-soft outcome sync + orphan reconciliation run on a fresh store, so
    pr_state/orphans keep advancing even when no terminal CLI command runs. The
    outcomes functions are themselves fail-soft; ``SweepWorker`` also guards.

    What is new (#2297): the cycle returns its counts, so a sweep whose every
    ``gh`` call failed no longer records exactly what a clean one did. It writes
    the ``scheduler_heartbeat`` meta key ``health.check_dispatch_state``
    thresholds (nothing wrote it before, so a dead sweep read as healthy), it
    snapshots the epic driver locks for the serve-side liveness readings that
    cannot open them (``refresh_driver_locks``), and it takes the free rate-limit
    reading once per cycle — that endpoint spends no quota and doubles as the
    auth probe — caching it in meta so no request path ever makes a GitHub call
    for it.

    ``readings`` carries the outcome sync's PR readings from one cycle to the
    next, so the sync makes one batched GraphQL request a cycle and skips PRs
    that have not moved (#2750); the cycle reports that spend.
    """
    from kew.github_rate_limit import read_rate_limit
    from kew.outcomes import reconcile_orphan_prs, sync_outcomes
    from kew.serve.sweep import SweepCycleResult

    with store_factory() as store:
        synced = sync_outcomes(store, config, readings=readings)
        listed = readings.open_prs if readings is not None else None
        if listed is None:
            reconcile_orphan_prs(store, config)
        else:
            reconcile_orphan_prs(store, config, open_prs=listed)
        refresh_driver_locks(store, config)
        reading = read_rate_limit()
        store.set_meta("github_rate_limit", _json.dumps(reading.as_dict()))
        store.set_meta("scheduler_heartbeat", _now_iso())
    _warm_health_probes(health_reader, config)
    return SweepCycleResult(
        synced=synced.synced,
        no_pr=synced.no_pr,
        query_failures=synced.query_failures,
        rate_limit=reading.as_dict(),
        graphql_calls=synced.graphql_calls,
    )


def _warm_health_probes(health_reader: Any | None, config: DispatchConfig) -> None:
    """Refresh the health page's host probes on the cycle's back (#2298).

    The slow probes — is the container runtime there, is each routed image
    present — belong to the sweep rather than to a request: a tab open on
    ``/health`` must not shell out to docker every few seconds. Fail-soft and
    last, so a probe that hangs or raises cannot cost the cycle its outcome sync
    or its heartbeat.
    """
    if health_reader is None:
        return
    try:
        health_reader.warm(config)
    except Exception:  # noqa: BLE001 — a probe must never break the cycle
        _logger.warning("sweep: health probe warm failed", exc_info=True)


def _serve_resolve_bind(
    config: DispatchConfig, host: str, port: int, expose: bool
) -> tuple[str, int, bool]:
    """Resolve the effective bind host/port/expose and reject unsafe binds.

    Raises ``typer.Exit(2)`` for a non-loopback bind without --expose, or a
    wildcard address when exposing (the browser Host/Origin allowlist needs the
    concrete address). Behavior is unchanged from the inline checks.
    """
    bind_host = host or config.flight_recorder.host
    bind_port = port or config.flight_recorder.port
    effective_expose = expose or config.flight_recorder.expose

    if not _is_loopback(bind_host) and not effective_expose:
        _error(
            f"[flight_recorder].host={bind_host!r} is non-loopback; "
            "pass --expose (or set [flight_recorder].expose = true) to bind it."
        )
        raise typer.Exit(2)
    # B104: this REJECTS a wildcard bind (kew never binds to it); the literal
    # "0.0.0.0" trips bandit, suppressed inline on the finding line.
    if effective_expose and bind_host in ("0.0.0.0", "::", ""):  # nosec B104
        _error(
            f"[flight_recorder].host={bind_host!r} is a wildcard; exposing needs the "
            "concrete address clients connect to (the browser Host/Origin allowlist "
            "requires it) — bind that address, or front kew with a reverse proxy."
        )
        raise typer.Exit(2)
    return bind_host, bind_port, effective_expose


def _serve_check_transport(config: DispatchConfig, expose: bool) -> None:
    """Refuse an exposed bind whose transport nobody declared (W1.6).

    Imported lazily: ``kew.serve.security`` pulls in FastAPI, which the base
    install does not have — every other ``kew`` command must keep importing
    this module.
    """
    if not expose:
        return
    from kew.serve.security import expose_transport_refusal

    refusal = expose_transport_refusal(config.flight_recorder.trusted_proxies)
    if refusal is not None:
        _error(refusal)
        raise typer.Exit(2)


def _serve_check_db_schema(db_path: Path) -> None:
    """Read-only diagnostic check for a schema newer than this kew supports."""
    if not db_path.exists():
        return
    try:
        SqliteRunStore(str(db_path), read_only=True).close()
    except SchemaTooNewError as exc:
        _error(
            f"{exc}\n       Fix with:\n         {UPGRADE_HINT}\n       (db: {db_path})"
        )
        raise typer.Exit(2) from exc


def _open_serve_store(*, project_root: Path, db_path: Path) -> SqliteRunStore:
    """Open serve's writable request store through the ledger registry."""
    root = canonical_project_root(project_root)
    return open_managed_store(
        project_root=root,
        db_path=db_path.resolve(),
        anchors_path=root / DEFAULT_ANCHORS_PATH,
        read_only=False,
        check_same_thread=True,
    )


def _pin_project_repo(config: DispatchConfig) -> None:
    """Resolve an unset ``project.repo`` the way ``kew merge queue`` does.

    The queue's commands store a stop under the slug they detect, so a console
    serving the blank config would look up no repo and hide it (#2883). A
    failed detection leaves it unset: the merge-queue source then serves every
    stop in the ledger and names itself unavailable rather than empty.
    """
    with contextlib.suppress(typer.Exit):
        _c._detect_repo(config)
    if not config.project.repo:
        _warn(
            "Could not detect this project's repo; set project.repo in kew.toml "
            "so the console can tell which merge queue is this project's."
        )


@serve_app.callback(invoke_without_command=True)
def serve(
    ctx: typer.Context,
    host: Annotated[
        str,
        typer.Option(
            help="Bind host (default from \\[flight_recorder].host)",
            show_default=False,
        ),
    ] = "",
    port: Annotated[
        int,
        typer.Option(
            help="Bind port (default from \\[flight_recorder].port)",
            show_default=False,
        ),
    ] = 0,
    expose: Annotated[
        bool,
        typer.Option(
            help="Allow binding a non-loopback host (needs a declared TLS proxy "
            "in \\[flight_recorder].trusted_proxies)"
        ),
    ] = False,
    new_token: Annotated[
        bool, typer.Option("--new-token", help="Rotate the serve token")
    ] = False,
) -> None:
    """Serve the kew web UI + API + approval inbox on localhost."""
    if ctx.invoked_subcommand is not None:
        return
    _c._emit_staleness_warnings()
    try:
        import uvicorn

        from kew.serve.app import create_app
    except ModuleNotFoundError:
        _error(
            "kew serve needs the 'serve' extra. Install: pip install 'kew-cli[serve]'"
        )
        raise typer.Exit(2) from None

    import os

    from kew.serve.auth import SessionStore, resolve_token
    from kew.serve.security import forwarded_header_trust
    from kew.service.runs import build_meta

    config = _c.load_config()
    _c.preflight_tools()
    _pin_project_repo(config)

    bind_host, bind_port, effective_expose = _serve_resolve_bind(
        config, host, port, expose
    )
    _serve_check_transport(config, effective_expose)
    # Forwarded headers are believed only from the declared proxies — uvicorn
    # would otherwise trust 127.0.0.1's X-Forwarded-* by default.
    proxy_headers, forwarded_allow_ips = forwarded_header_trust(
        config.flight_recorder.trusted_proxies
    )

    log_dir = Path(config.logging.dir).resolve()
    log_dir.mkdir(parents=True, exist_ok=True)
    db_path = log_dir / "dispatch.db"

    _serve_check_db_schema(db_path)

    if new_token and os.environ.get("KEW_SERVE_TOKEN"):
        _warn(
            "--new-token has no effect while KEW_SERVE_TOKEN is set (the env token is "
            "authoritative); unset it to rotate the file-based token."
        )
    token = resolve_token(
        log_dir / "serve_token", os.environ.get("KEW_SERVE_TOKEN"), regenerate=new_token
    )

    # The reviewer copy the console previews is found from the project, as the
    # review launch and `kew prompts` find it, never from the cwd (#3108). The
    # ledger root is the drive's own: canonical_project_root(project_root()).
    served_project = _project_root()
    repo_dir = _policy_repo_root(served_project)
    project_root = canonical_project_root(served_project)
    static_dir = Path(__file__).parent.parent / "serve" / "web"
    # Source tree used for the bundle-staleness check: exists only in a kew
    # source checkout (parents[3] is the repo root); a wheel install has none,
    # so staleness is silently skipped there.
    frontend_src = Path(__file__).parents[3] / "frontend" / "src"

    meta = build_meta(
        mode=config.billing.mode,
        plan_label=config.billing.plan_label,
        plan_monthly_usd=config.billing.plan_monthly_usd,
        version=_c._resolve_version(),
    )

    def _serve_store_factory() -> SqliteRunStore:
        from fastapi import HTTPException

        try:
            return _open_serve_store(project_root=project_root, db_path=db_path)
        except SchemaTooNewError as exc:
            raise HTTPException(
                status_code=503,
                detail=f"{exc}\nFix: {UPGRADE_HINT}",
            ) from exc

    from kew.cli.doctor import run_checks
    from kew.outcomes import PRReadings
    from kew.prompt_previews import PREVIEW_RENDERERS
    from kew.serve.sweep import SweepWorker
    from kew.service.health import HealthReader

    health_reader = HealthReader()
    pr_readings = PRReadings()
    application = create_app(
        _serve_store_factory,
        repo_dir=repo_dir,
        static_dir=static_dir if static_dir.is_dir() else None,
        meta=meta,
        config=config,
        token=token,
        sessions=SessionStore(),
        bind_host=bind_host,
        bind_port=bind_port,
        expose=effective_expose,
        extra_origins=config.flight_recorder.extra_origins,
        sweep=SweepWorker(
            lambda: sweep_cycle(
                _serve_store_factory,
                config,
                health_reader=health_reader,
                readings=pr_readings,
            )
        ),
        frontend_dir=frontend_src,
        health_reader=health_reader,
        readiness_checks=lambda: run_checks(config),
        prompt_renderers=PREVIEW_RENDERERS,
    )
    from kew.serve.bundle import startup_warnings

    for warning in startup_warnings(application.state.bundle_status):
        _warn(warning)
    (log_dir / "serve_state.json").write_text(
        _json.dumps({"host": bind_host, "port": bind_port})
    )
    _echo_startup_urls(
        bind_host, bind_port, token, env_token=bool(os.environ.get("KEW_SERVE_TOKEN"))
    )
    uvicorn.run(
        application,
        host=bind_host,
        port=bind_port,
        proxy_headers=proxy_headers,
        forwarded_allow_ips=forwarded_allow_ips,
    )


def _echo_startup_urls(host: str, port: int, token: str, *, env_token: bool) -> None:
    """Print where kew serve listens; the tokened link only to a terminal.

    A service manager or CI job writes stdout to a log, and the token in that
    log is the admin credential. Off a terminal, ``kew serve url`` prints it
    from the token file, which kew writes only when ``KEW_SERVE_TOKEN`` is
    unset; with it set, the pointer names the variable, never its value.
    """
    typer.echo(f"kew serve → http://{host}:{port}/")
    if sys.stdout.isatty():
        typer.echo(f"http://{host}:{port}/#token={token}")
    elif env_token:
        typer.echo(
            f"Sign in at http://{host}:{port}/#token=<value of KEW_SERVE_TOKEN>."
        )
    else:
        typer.echo("Run `kew serve url` for the link that signs you in.")


@serve_app.command("url")
def serve_url() -> None:
    """Print the access URL (with token) for the running kew serve instance."""
    import json as _json
    import os

    config = _c.load_config()
    log_dir = Path(config.logging.dir).resolve()
    state_file = log_dir / "serve_state.json"
    token_file = log_dir / "serve_token"

    if not state_file.exists():
        _error(
            "kew serve is not running (no serve state found). Start it with: kew serve"
        )
        raise typer.Exit(2)

    try:
        state = _json.loads(state_file.read_text())
        host = state["host"]
        port = state["port"]
    except (KeyError, ValueError, OSError) as exc:
        _error(f"Could not read serve state: {exc}")
        raise typer.Exit(2) from exc

    env_token = os.environ.get("KEW_SERVE_TOKEN")
    if env_token:
        token = env_token
    elif token_file.exists():
        token = token_file.read_text().strip()
        if not token:
            _error(
                "kew serve is not running (empty token file). Start it with: kew serve"
            )
            raise typer.Exit(2)
    else:
        _error(
            "kew serve is not running (no serve token found). Start it with: kew serve"
        )
        raise typer.Exit(2)

    typer.echo(f"http://{host}:{port}/#token={token}")
