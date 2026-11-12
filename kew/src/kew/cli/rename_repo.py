"""`kew rename-repo`: move the ledger's rows from an old repository slug to the
configured ``project.repo`` (#3527).

A one-time command for a repository GitHub renamed. The ledger rewrite and its
refusals live in :mod:`kew.run_store_rename`; this module checks the new
repository exists and that GitHub still answers the old name with it (unless
``--from-name-reused``), reads the refusals that live outside the ledger (the
drive, a serve process, epic driver lock files), prints the plan, and after
the commit moves the run-report sink files (:mod:`kew.run_report_rename`). A
dry run, the default, opens the ledger read-only and writes nothing.
"""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, NoReturn

import typer

from kew import cli as _c
from kew import drive, subprocess_utils
from kew.audit import resolve_actor
from kew.cli import epic as _epic
from kew.command_outcome import COULD_NOT_RUN_EXIT_CODE
from kew.config import DispatchConfig
from kew.health import STALE_SWEEP_SECONDS
from kew.progress import _error, _success, _warn
from kew.repo_host import repo_host
from kew.run_report_rename import RenamedValues, RepoReportMove, move_repo_reports
from kew.run_report_sink import repo_report_dir, repo_reports
from kew.run_store import SqliteRunStore
from kew.run_store_rename import (
    UNTOUCHED_TABLES,
    RepoRenamePlan,
    RepoRenameRefused,
    pr_prefix,
)
from kew.service.loops import list_loops

#: An ``OWNER/NAME`` slug: each segment starts with a letter or digit, so no
#: segment is ``.`` or ``..`` and none reads as a path out of the sink.
_SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*")


def _target(config: DispatchConfig, from_repo: str) -> str:
    """``project.repo``, the slug the rows move to; exits when it cannot be."""
    to_repo = config.project.repo.strip()
    if not _SLUG.fullmatch(from_repo):
        _error(f"kew rename-repo: --from {from_repo!r} is not an OWNER/NAME slug")
        raise typer.Exit(COULD_NOT_RUN_EXIT_CODE)
    if not to_repo:
        _error(
            "kew rename-repo: project.repo is empty; set it in kew.toml to the "
            "repository's new OWNER/NAME first"
        )
        raise typer.Exit(COULD_NOT_RUN_EXIT_CODE)
    if to_repo == from_repo:
        _error(
            f"kew rename-repo: project.repo is still {from_repo}; set it in "
            "kew.toml to the repository's new OWNER/NAME first"
        )
        raise typer.Exit(COULD_NOT_RUN_EXIT_CODE)
    if to_repo.casefold() == from_repo.casefold():
        # GitHub names ignore case, and so may the file system: the two sink
        # directories would be one, and moving each report would delete it.
        _error(
            f"kew rename-repo: project.repo {to_repo} differs from {from_repo} "
            "only in case; GitHub treats them as one name, so there is nothing "
            "to rename"
        )
        raise typer.Exit(COULD_NOT_RUN_EXIT_CODE)
    return to_repo


def _github_name(to_repo: str) -> str:
    """The ``full_name`` GitHub answers for *to_repo*; raises
    :class:`ValueError` saying why there is none."""
    try:
        result = subprocess_utils.run(["gh", "api", f"repos/{to_repo}"], check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError(f"gh could not run: {exc}") from exc
    if result.returncode != 0:
        raise ValueError((result.stderr or "").strip() or "gh exited non-zero")
    try:
        name = json.loads(result.stdout or "{}").get("full_name")
    except (ValueError, AttributeError) as exc:
        raise ValueError(
            f"gh answered something other than a repository: {exc}"
        ) from exc
    if not isinstance(name, str):
        raise ValueError("gh answered no full_name")
    return name


def _confirm_on_github(to_repo: str) -> None:
    """Exit unless GitHub knows *to_repo* by exactly that name."""
    try:
        name = _github_name(to_repo)
    except ValueError as exc:
        found = str(exc)
    else:
        if name == to_repo:
            return
        found = f"it answers {name}"
    _error(
        f"kew rename-repo: GitHub has no repository {to_repo} ({found}, for "
        f"`gh api repos/{to_repo}`); nothing was changed"
    )
    raise typer.Exit(COULD_NOT_RUN_EXIT_CODE)


def _old_name_refusal(from_repo: str, to_repo: str) -> str | None:
    """Why GitHub does not prove *from_repo* and *to_repo* one repository.

    GitHub redirects a renamed repository's old name to the new one until
    another repository takes the name, so ``gh api repos/FROM`` answering
    *to_repo* proves the rename. None when it does.
    """
    try:
        name = _github_name(from_repo)
    except ValueError as exc:
        found = f"it does not answer ({exc})"
    else:
        if name == to_repo:
            return None
        found = f"it answers {name}"
    return (
        f"GitHub does not show {from_repo} as {to_repo} (`gh api repos/{from_repo}`"
        f": {found}), so kew cannot tell they are one repository. If another"
        f" repository has taken the old name since, pass --from-name-reused to"
        f" vouch that the ledger's {from_repo} rows are {to_repo}'s"
    )


def _require_ledger(config: DispatchConfig) -> Path:
    db_path = Path(config.logging.dir).resolve() / "dispatch.db"
    if not db_path.is_file():
        _error(f"kew rename-repo: no run ledger at {db_path}")
        raise typer.Exit(COULD_NOT_RUN_EXIT_CODE)
    return db_path


def _serve_live(stamp: str | None, now: datetime) -> bool:
    """Whether the serve sweep's heartbeat is fresh. An unreadable stamp is
    never proven old, so it reads live."""
    if not stamp:
        return False
    try:
        beat = datetime.fromisoformat(stamp)
    except ValueError:
        return True
    if beat.tzinfo is None:
        beat = beat.replace(tzinfo=UTC)
    return (now - beat).total_seconds() <= STALE_SWEEP_SECONDS


def _driving_loops(store: SqliteRunStore, from_repo: str, now: datetime) -> list[str]:
    """The pursuing loops of *from_repo* the console does not read as stale.

    The console's own reading (:func:`kew.service.loops.list_loops`): a loop
    is stale once quiet past its window since its newest activity stamp, and a
    stamp that is present but unreadable proves no silence, so that loop
    counts as driving. A paused loop is stopped on purpose (its row moves
    with the rest, so a resume acts on the new name); a stale one is history;
    a loop of another repository, or with no repo, does not act on this one.
    """
    return [
        loop.loop_id
        for loop in list_loops(store, status="pursuing", now=now)
        if loop.repo == from_repo and not loop.stale
    ]


def _outside_refusals(
    config: DispatchConfig, store: SqliteRunStore, now: datetime, from_repo: str
) -> list[str]:
    """What outside the ledger's tables must stop before the rename."""
    found: list[str] = []
    loops = _driving_loops(store, from_repo, now)
    if loops:
        found.append(f"a loop of {from_repo} is pursuing: {', '.join(loops)}")
    holder = drive.live_holder(store, now=now)
    if holder is not None:
        found.append(f"a drive holds its lock (pid {holder.pid} on {holder.host})")
    if _serve_live(store.get_meta("scheduler_heartbeat"), now):
        window = f"{STALE_SWEEP_SECONDS:.0f}s"
        found.append(f"a serve process is live (its sweep beat within {window})")
    held = [p.name for p in _epic._epic_lock_files(config) if _c.lock_holder(p)]
    if held:
        found.append(f"an epic driver lock is held: {', '.join(held)}")
    return found


def _print_counts(heading: str, counts: dict[str, int]) -> None:
    typer.echo(heading)
    for column, count in counts.items():
        typer.echo(f"  {column}: {count}")


def _print_plan(
    config: DispatchConfig,
    plan: RepoRenamePlan,
    reports: list[Path],
    refusals: list[str],
) -> None:
    """The dry run: what --apply would rewrite and move, and what stops it."""
    _print_counts(f"Would rewrite {plan.from_repo} -> {plan.to_repo}:", plan.counts)
    target = repo_report_dir(config, plan.to_repo)
    typer.echo(
        f"Would move {len(reports)} run report(s) from "
        f"{repo_report_dir(config, plan.from_repo)} to {target}"
    )
    typer.echo(
        f"Never touched: {', '.join(UNTOUCHED_TABLES)}; one repo_renamed audit "
        "event is appended instead."
    )
    if plan.renamed:
        typer.echo(f"A repo_renamed event from {plan.from_repo} already exists.")
    for reason in refusals:
        _warn(f"--apply would refuse now: {reason}")
    typer.echo("Dry run: nothing was written. Pass --apply to rename.")


def _move_reports(
    config: DispatchConfig, plan: RepoRenamePlan, only: frozenset[str] | None
) -> RepoReportMove:
    """Move the old slug's run reports, rewriting their repo and PR link;
    with *only*, just the reports of those run ids."""
    values = RenamedValues(
        plan.from_repo,
        plan.to_repo,
        pr_prefix(plan.host, plan.from_repo),
        pr_prefix(plan.host, plan.to_repo),
    )
    move = move_repo_reports(
        repo_report_dir(config, plan.from_repo),
        repo_report_dir(config, plan.to_repo),
        values,
        only=only,
    )
    if move.moved:
        typer.echo(f"Moved {len(move.moved)} run report(s).")
    for problem in move.kept:
        _error(f"Run report not moved: {problem}")
    return move


def _refuse(reason: str) -> NoReturn:
    _error(f"kew rename-repo refused: {reason}")
    _error("Nothing was written.")
    raise typer.Exit(COULD_NOT_RUN_EXIT_CODE)


def _apply(
    config: DispatchConfig, store: SqliteRunStore, plan: RepoRenamePlan, now: datetime
) -> None:
    """Commit the rename, or exit 2 with nothing written."""
    try:
        result = store.rename_repo(
            plan.from_repo,
            plan.to_repo,
            host=plan.host,
            actor_id=resolve_actor().id,
            now=now,
            guard=lambda: _outside_refusals(config, store, now, plan.from_repo),
        )
    except RepoRenameRefused as exc:
        for reason in exc.reasons:
            _error(f"kew rename-repo refused: {reason}")
        _error("Nothing was written. Stop what is named above and run it again.")
        raise typer.Exit(COULD_NOT_RUN_EXIT_CODE) from exc
    except sqlite3.Error as exc:
        _error(f"kew rename-repo: the ledger rolled back, nothing was written: {exc}")
        raise typer.Exit(1) from exc
    _print_counts(f"Rewrote {plan.from_repo} -> {plan.to_repo}:", result.counts)
    _success(f"Audit event {result.event_id} (repo_renamed) records the rename.")


def rename_repo(
    from_repo: Annotated[
        str,
        typer.Option("--from", help="The repository's old OWNER/NAME slug."),
    ],
    apply: Annotated[
        bool,
        typer.Option("--apply", help="Rewrite the ledger; without it, a dry run."),
    ] = False,
    from_name_reused: Annotated[
        bool,
        typer.Option(
            "--from-name-reused",
            help=(
                "Another repository has taken the old name: skip the check that "
                "GitHub redirects it to project.repo, and vouch for it yourself."
            ),
        ),
    ] = False,
) -> None:
    """Move the ledger's rows from an old repository slug to project.repo."""
    config = _c.load_config()
    to_repo = _target(config, from_repo)
    subprocess_utils.preflight_tools(required=("gh",))
    _confirm_on_github(to_repo)
    name_refusal = None if from_name_reused else _old_name_refusal(from_repo, to_repo)
    if apply and name_refusal is not None:
        _refuse(name_refusal)
    db_path = _require_ledger(config)
    now = datetime.now(UTC)
    with _c._open_run_store(str(db_path), read_only=not apply) as store:
        plan = store.plan_repo_rename(from_repo, to_repo, host=repo_host(), now=now)
        reports = repo_reports(config, from_repo)
        if not apply:
            refusals = _dry_run_refusals(config, store, plan, reports, now)
            if name_refusal is not None:
                refusals.insert(0, name_refusal)
            _print_plan(config, plan, reports, refusals)
            return
        if not _ledger_step(config, store, plan, reports, now):
            return
        only = _ledger_runs(store, reports, to_repo) if from_name_reused else None
    if _move_reports(config, plan, only).kept:
        raise typer.Exit(1)


def _nothing_holds(from_repo: str) -> str:
    return (
        f"nothing holds {from_repo}: no ledger row, no run report and no earlier"
        " rename. Check the --from spelling"
    )


def _dry_run_refusals(
    config: DispatchConfig,
    store: SqliteRunStore,
    plan: RepoRenamePlan,
    reports: list[Path],
    now: datetime,
) -> list[str]:
    """What would refuse ``--apply`` in this state, exactly as it decides.

    With the rename recorded and no old-slug rows, ``--apply`` only moves the
    reports left, which no outside blocker stops.
    """
    refusals = list(plan.refusals)
    if not plan.renamed and not plan.rows and not reports:
        refusals.append(_nothing_holds(plan.from_repo))
    if plan.rows or not plan.renamed:
        refusals.extend(_outside_refusals(config, store, now, plan.from_repo))
    return refusals


def _ledger_runs(
    store: SqliteRunStore, reports: list[Path], to_repo: str
) -> frozenset[str]:
    """The run ids of *reports* this ledger records under *to_repo*.

    With ``--from-name-reused`` GitHub no longer proves the old name is this
    repository, and the shared sink's old-slug directory may hold reports of
    the repository that took it: only this ledger's runs are vouched for.
    """
    owned = set()
    for path in reports:
        record = store.get(path.stem)
        if record is not None and record.repo == to_repo:
            owned.add(path.stem)
    return frozenset(owned)


def _ledger_step(
    config: DispatchConfig,
    store: SqliteRunStore,
    plan: RepoRenamePlan,
    reports: list[Path],
    now: datetime,
) -> bool:
    """Commit the rename when one is due; whether reports are left to move.

    After a recorded rename only the reports an earlier run could not move
    remain; old-slug rows that reappeared are refused by the store.
    """
    if not plan.renamed and not plan.rows and not reports:
        _refuse(_nothing_holds(plan.from_repo))
    if plan.renamed and not plan.rows and not reports:
        typer.echo(
            f"Nothing to do: {plan.from_repo} was already renamed to "
            f"{plan.to_repo}, and no row or run report holds it."
        )
        return False
    if plan.rows or not plan.renamed:
        _apply(config, store, plan, now)
    return True
