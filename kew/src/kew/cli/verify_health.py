"""``kew verify nightly``, ``kew verify main-health`` (#3195) and
``kew verify report`` (#3215).

Both are words ``kew verify`` reads in place of a ref, so ``kew verify REF``
keeps its meaning: ``nightly`` runs the nightly full verify of main (``install``
and ``uninstall`` manage its launchd agent), and ``main-health`` prints main's
health (``clear SHA --issue N --reason …`` reads it green), and ``report``
prints what every red verify was, phase by phase (:mod:`kew.verify_ledger`).
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import NoReturn

import typer

from kew import cli as _c
from kew.config import project_root
from kew.progress import _error, _success, _warn
from kew.runner_fields import exception_text

#: The words ``kew verify`` reads as a subcommand rather than a ref.
SUBCOMMANDS = ("nightly", "main-health", "report")


def _usage(text: str) -> NoReturn:
    _error(f"kew verify: {text}")
    raise typer.Exit(2)


def run_subcommand(
    word: str,
    rest: list[str],
    *,
    issue: int | None,
    reason: str | None,
    kew_binary: Path | None,
    env_file: Path | None,
    since: str = "30d",
) -> None:
    """Dispatch ``kew verify nightly …``, ``main-health …`` and ``report``."""
    if word == "nightly":
        _nightly(rest, kew_binary=kew_binary, env_file=env_file)
    elif word == "report":
        _report(rest, since)
    elif not rest:
        _show_health()
    elif rest[0] == "clear" and len(rest) == 2:
        _clear(rest[1], issue=issue, reason=reason)
    else:
        _usage("usage: kew verify main-health [clear SHA --issue N --reason TEXT]")


def _nightly(
    rest: list[str], *, kew_binary: Path | None, env_file: Path | None
) -> None:
    if not rest:
        _run_nightly()
    elif rest == ["install"]:
        _install(kew_binary, env_file)
    elif rest == ["uninstall"]:
        _uninstall()
    else:
        _usage("usage: kew verify nightly [install|uninstall]")


def _report(rest: list[str], since: str) -> None:
    """``kew verify report``: reds by phase × class, and phase seconds."""
    from kew.verify_ledger import parse_since, report

    if rest:
        _usage("usage: kew verify report [--since 30d]")
    try:
        span = parse_since(since)
    except ValueError as exc:
        _usage(f"report: {exc}")
    with _c._open_run_store(str(_store_path())) as store:
        typer.echo(report(store, since=span), nl=False)


def _store_path() -> Path:
    config = _c.load_config()
    log_dir = Path(config.logging.dir).resolve()
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / "dispatch.db"


def _run_nightly() -> None:
    from kew.main_health import NIGHTLY_BUSY_EXIT, RED, MainHealthError
    from kew.merge_verify import VerifyRefused, summary_line, terminate_unwinds
    from kew.nightly import NightlyBusy, nightly_summary, run_nightly

    # The nightly files its issues with ``gh``: a missing one is refused here,
    # before the lease and the hour-long verify, not as a traceback after it.
    _c.preflight_tools(required=("git", "gh"))
    with terminate_unwinds(), _c._open_run_store(str(_store_path())) as store:
        config = _c.load_config(store=store)
        try:
            report = run_nightly(config, store=store, repo_root=project_root())
        except NightlyBusy as exc:
            _error(f"kew verify nightly: {exc}")
            raise typer.Exit(NIGHTLY_BUSY_EXIT) from None
        except (VerifyRefused, MainHealthError) as exc:
            _error(f"kew verify nightly: {exception_text(exc)}")
            raise typer.Exit(2) from None
    line = f"kew verify nightly {summary_line(report.candidate, report.result)}"
    (_success if report.verdict == "passed" else _error)(line)
    for text in nightly_summary(report):
        typer.echo(f"kew verify nightly: {text}")
    if report.verdict == "incomplete":
        # No exit, a kill, a broken host environment or a refused preflight:
        # the job never ran its gates, which says nothing about main's health.
        _error(
            "kew verify nightly: incomplete: the job never ran its gates, "
            "main's health unchanged"
        )
        raise typer.Exit(2)
    if report.verdict == RED:
        raise typer.Exit(1)


def _base_ref() -> tuple[object, Path, str]:
    from kew.main_health import base_ref_for

    config = _c.load_config()
    root = project_root()
    return config, root, base_ref_for(root, config.branches.base or "main")


def _show_health() -> None:
    from kew.main_health import MainHealthError, read_main_health

    # Health is read off main's history with ``git``: registered here, as
    # every command that shells out registers what it runs.
    _c.preflight_tools(required=("git",))
    try:
        with _c._open_run_store(str(_store_path())) as store:
            _config, root, base_ref = _base_ref()
            health = read_main_health(store, root, base_ref=base_ref)
    except MainHealthError as exc:
        _error(f"kew verify main-health: {exc}")
        raise typer.Exit(2) from None
    typer.echo(f"kew verify main-health: {health.describe()}")
    if health.red:
        raise typer.Exit(1)


def _clear(sha: str, *, issue: int | None, reason: str | None) -> None:
    from kew.main_health import MainHealthError, clear, read_main_health

    if issue is None or not (reason or "").strip():
        _usage("main-health clear needs --issue N and --reason TEXT")
    _c.preflight_tools(required=("git",))
    try:
        with _c._open_run_store(str(_store_path())) as store:
            config, root, base_ref = _base_ref()
            row = clear(
                store,
                root,
                sha=sha,
                issue=int(issue or 0),
                reason=str(reason),
                base_ref=base_ref,
                repo=getattr(getattr(config, "project", None), "repo", None) or None,
            )
            health = read_main_health(store, root, base_ref=base_ref)
    except MainHealthError as exc:
        _error(f"kew verify main-health clear: {exc}")
        raise typer.Exit(2) from None
    _success(f"kew verify main-health: cleared {sha[:12]} (audit row {row})")
    typer.echo(f"kew verify main-health: {health.describe()}")


def _install(kew_binary: Path | None, env_file: Path | None) -> None:
    from kew.cli.drive import _kew_binary
    from kew.config import config_environment
    from kew.drive_agent import (
        AgentRefused,
        checked_binary,
        checked_env_file,
        launchd_available,
        prepare_log,
    )
    from kew.nightly_agent import NIGHTLY_LOG, NightlySpec, install

    if not launchd_available():
        _usage("nightly install: launchd agents need macOS with launchctl")
    db = _store_path()
    try:
        sourced = None if env_file is None else checked_env_file(env_file)
        binary = checked_binary(_kew_binary(kew_binary))
        prepare_log(db.parent / NIGHTLY_LOG)
        spec = NightlySpec(
            project=project_root(),
            binary=binary,
            drive_args=(),
            log=db.parent / NIGHTLY_LOG,
            env_file=sourced,
            environment=config_environment(os.environ, Path.cwd()),
        )
        plist = install(spec)
    except (AgentRefused, OSError, RuntimeError, sqlite3.Error) as exc:
        _usage(f"nightly install: {exc}")
    _success(
        f"kew verify nightly: installed {spec.label} at {plist}; it verifies "
        "main in full every day at 01:30."
    )


def _uninstall() -> None:
    from kew.drive_agent import AgentRefused, launchd_available
    from kew.nightly_agent import uninstall

    if not launchd_available():
        _usage("nightly uninstall: launchd agents need macOS with launchctl")
    try:
        removed = uninstall(project_root())
    except (AgentRefused, OSError, RuntimeError) as exc:
        _usage(f"nightly uninstall: {exc}")
    if removed is None:
        _warn("kew verify nightly: no nightly agent is installed for this project.")
        return
    _success(f"kew verify nightly: removed {removed}")
