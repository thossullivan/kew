"""`kew gc`: list, and with ``--apply`` remove, what closed work left on disk
(#2417).

The selection and removal live in :mod:`kew.gc`; this module reads the two
inputs that need the CLI's surroundings — the run ledger, and doctor's agent
image inventory — and prints the plan. A dry run, the default, changes nothing
and opens the ledger read-only.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Annotated

import typer

from kew import cli as _c
from kew import gc as _gc
from kew import subprocess_utils
from kew.cli import doctor as _doctor
from kew.command_outcome import COULD_NOT_RUN_EXIT_CODE
from kew.config import DispatchConfig, project_root
from kew.progress import _error, _success, _warn
from kew.publication_apply import trusted_git_env
from kew.run_store import RunStore
from kew.sandbox import detect_runtime

_UNITS = ("B", "KB", "MB", "GB", "TB")


def _human(size: int) -> str:
    value = float(size)
    for unit in _UNITS:
        if value < 1024 or unit == _UNITS[-1]:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    raise AssertionError("unreachable")


def _counted(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _stale_agent_tags(config: DispatchConfig) -> tuple[list[_gc.AgentTag], str | None]:
    """Doctor's stale agent tags, or why they could not be read."""
    sandbox = config.sandbox
    if detect_runtime(sandbox) is None:
        return [], (
            f"agent image inventory could not be read (runtime {sandbox.runtime!r} "
            "was not found)"
        )
    try:
        stale = _doctor._stale_retained_agent_tags(sandbox)
    except _doctor._RetainedTagInventoryError as exc:
        return [], f"agent image inventory could not be read ({exc})"
    return [_gc.AgentTag(tag.reference, tag.logical_size) for tag in stale], None


def _primary_checkout(project: Path) -> Path | str:
    """The git checkout *project* is in, read from *project* (never from the
    cwd), or why git could not name one."""
    try:
        result = subprocess_utils.run(
            ["git", "-C", str(project), "rev-parse", "--show-toplevel"],
            check=False,
            env=trusted_git_env(),
        )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return f"git could not run ({exc})"
    root = (result.stdout or "").strip() if result.returncode == 0 else ""
    return Path(root) if root else "git names no checkout there"


def _print_plan(plan: _gc.Plan, *, apply: bool) -> None:
    verb = "Removing" if apply else "Would remove"
    typer.echo(f"{verb}:" if plan.sizes or plan.tags else "Nothing to remove.")
    for path, size in plan.sizes.items():
        typer.echo(f"  {_human(size):>9}  {path}")
    for tag in plan.tags:
        typer.echo(f"  {tag.size:>9}  image {tag.reference}")
    typer.echo(
        f"Total: {plan.total_bytes} bytes ({_human(plan.total_bytes)}) in "
        f"{_counted(len(plan.sizes), 'path')}, and "
        f"{_counted(len(plan.tags), 'agent image tag')} "
        "(image sizes are the runtime's logical sizes, not in the total)."
    )
    for reason, count in plan.kept.items():
        typer.echo(f"Kept {_counted(count, 'path')}: {reason}")
    for problem in plan.unreadable:
        _warn(f"Kept: {problem}")
    if not apply:
        typer.echo("Dry run: nothing was removed. Pass --apply to remove the above.")


def _report_outcome(outcome: _gc.Outcome) -> None:
    for removed in outcome.removed:
        typer.echo(f"Removed {removed}")
    for failed in outcome.failed:
        _error(f"Not removed: {failed}")
    if not outcome.failed:
        _success(f"Removed everything listed ({len(outcome.removed)} entries).")


def _remove(
    plan: _gc.Plan,
    config: DispatchConfig,
    store: RunStore,
    readers: tuple[_gc.TrackerState, _gc.OpenPrs],
    repo_root: Path,
) -> bool:
    """Remove *plan*, re-checking each hold under its claim; whether
    anything listed could not be removed."""
    tracker, open_prs = readers
    outcome = _gc.apply(
        plan,
        runtime=config.sandbox.runtime,
        repo_root=repo_root,
        hold=_gc.recheck(config, store, tracker, open_prs),
    )
    _report_outcome(outcome)
    return bool(outcome.failed)


def _require_ledger(config: DispatchConfig) -> Path:
    """The run ledger's path; exits when there is none."""
    db_path = Path(config.logging.dir).resolve() / "dispatch.db"
    if not db_path.is_file():
        _error(
            f"kew gc: no run ledger at {db_path}; without it kew cannot tell "
            "which checkouts a run still holds, so nothing is listed or removed"
        )
        raise typer.Exit(COULD_NOT_RUN_EXIT_CODE)
    return db_path


def _require_primary_checkout() -> Path:
    """The primary git checkout --apply unregisters worktrees from; exits
    when there is none."""
    repo_root = _primary_checkout(project_root())
    if isinstance(repo_root, str):
        # Without it a registered worktree would be deleted unregistered.
        _error(
            f"kew gc --apply: no git checkout found at {project_root()} "
            f"({repo_root}); without it kew cannot unregister a worktree, so "
            "nothing is removed"
        )
        raise typer.Exit(COULD_NOT_RUN_EXIT_CODE)
    return repo_root


def gc(
    apply: Annotated[
        bool,
        typer.Option("--apply", help="Remove exactly what the dry run lists."),
    ] = False,
    images: Annotated[
        bool,
        typer.Option(
            "--images",
            help="Also list, and with --apply remove, stale kew-agent image tags.",
        ),
    ] = False,
) -> None:
    """List what closed work left on disk; --apply removes it."""
    config = _c.load_config()
    # gh reads tracker state, git unregisters a registered worktree.
    subprocess_utils.preflight_tools(required=("gh", "git"))
    db_path = _require_ledger(config)
    repo_root = _require_primary_checkout() if apply else None
    _c._detect_repo(config)
    tags, tag_problem = _stale_agent_tags(config) if images else ([], None)
    repo = config.project.repo
    readers = (_gc.github_tracker(repo), _gc.github_open_prs(repo))
    failed = False
    with _c._open_run_store(str(db_path), read_only=not apply) as store:
        plan = _gc.select(
            config,
            store,
            readers[0],
            tags,
            open_prs=readers[1],
            project=_c.project_root(),
        )
        if tag_problem is not None:
            plan.record_unreadable(tag_problem)
        _print_plan(plan, apply=apply)
        if apply and repo_root is not None:
            failed = _remove(plan, config, store, readers, repo_root)
    if plan.unreadable:
        raise typer.Exit(COULD_NOT_RUN_EXIT_CODE)
    if failed:
        raise typer.Exit(1)
