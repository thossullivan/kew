"""`kew plan` command group: read the recorded plan for a work item (#2290).

Read-only. The plan is a ledger row the host wrote when the dispatch launched,
so this command opens the store read-only and prints what is there — it never
re-extracts from a live issue body, because that would report what the criteria
are NOW rather than what the item was dispatched against.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

from kew import cli as _c
from kew.cli._apps import plan_app
from kew.progress import _error
from kew.triage.schema import AcceptanceCriterion, ItemPlan

if TYPE_CHECKING:
    from kew.run_store import ItemPlanRow


@plan_app.command("show")
def plan_show(
    item: Annotated[
        str,
        typer.Argument(
            help="Work item: a GitHub issue number (2278) or a tracker id (SFD-12)"
        ),
    ],
    history: Annotated[
        bool,
        typer.Option("--history", help="Also list the superseded plans, oldest first."),
    ] = False,
) -> None:
    """Print an item's recorded plan: its sha, source and acceptance criteria."""
    config = _c.load_config()
    db_path = Path(config.logging.dir).resolve() / "dispatch.db"
    if not db_path.is_file():
        _error(f"kew plan show: no ledger at {db_path}")
        raise typer.Exit(2)
    current, rows = _read_plans(db_path, item, history=history)
    if current is None:
        # A recorded "no criteria" plan is a row with criteria_count = 0. No row
        # at all means no dispatch on this ledger ever read this item's body,
        # which is a different answer and must not read as "it has no criteria".
        _error(f"kew plan show: no plan recorded for {item}")
        raise typer.Exit(2)
    for row in rows:
        if row.superseded_at is not None:
            _echo_plan(row, label="superseded")
            typer.echo("")
    _echo_plan(current, label="current")


def _read_plans(
    db_path: Path, item: str, *, history: bool
) -> tuple[ItemPlanRow | None, list[ItemPlanRow]]:
    """The item's current plan and (when asked) its history.

    The read-only open cannot migrate the ledger, so a ledger written before
    ``item_plans`` existed reaches the query as a plain "no such table" —
    sqlite3 raises it, and ``SchemaTooNewError`` never fires because that only
    rejects NEWER schemas. Refuse with the documented exit 2 rather than a
    traceback: "this ledger cannot record plans yet" is an answer, and the one
    thing it must not look like is a crash.
    """
    try:
        with _c._open_run_store(str(db_path), read_only=True) as store:
            return (
                store.current_item_plan(item),
                store.list_item_plans(item) if history else [],
            )
    except sqlite3.DatabaseError as exc:
        _error(
            f"kew plan show: cannot read plans from {db_path}: {exc}\n"
            "       A ledger older than recorded plans migrates on the next kew"
            " command that writes it; plans are recorded from the next dispatch on."
        )
        raise typer.Exit(2) from exc


def _echo_plan(row: ItemPlanRow, *, label: str) -> None:
    """Print one ``item_plans`` row: its identity lines, then its criteria."""
    typer.echo(
        f"{label}: {row.item_id}"
        f"  plan {row.plan_sha[:12]}"
        f"  source={row.source}"
        f"  criteria={row.criteria_count}"
        f"  recorded={row.recorded_at.isoformat()}"
    )
    typer.echo(f"  approved_by: {row.approved_by or '(not accepted by a person)'}")
    typer.echo(f"  body digest: {row.body_digest}")
    for criterion in _criteria(row.plan_json):
        typer.echo(f"  {criterion.id}: {criterion.statement}")


def _criteria(plan_json: str) -> list[AcceptanceCriterion]:
    """The row's criteria.

    An unreadable row refuses rather than printing a plan with no criteria:
    "the stored plan cannot be read" and "the item states no criteria" are
    different facts, and a caller must be able to tell them apart.
    """
    try:
        return list(ItemPlan.model_validate_json(plan_json).acceptance_criteria)
    except ValueError:
        _error("kew plan show: the stored plan JSON is unreadable")
        raise typer.Exit(2) from None
