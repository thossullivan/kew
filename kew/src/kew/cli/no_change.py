"""`kew no-change` command group: decide one no-change outcome (#2463).

A no-change hold waits on a person, and until this group existed the only way
to release one was the web console. That made the console a single point of
failure for an outcome that closes a tracker item with no diff behind it: an
operator whose console was refused, unreachable, or simply not running had no
way out of the hold short of editing ``[human_review].approvers`` — which also
governs human merge approval, so widening it to clear one hold widens who may
release every future merge.

The operator names the WORK ITEM, never the judgment's internal id. That id is
derived from the run and the attempt and no surface prints it, so a command
spelled with it would be unusable from a terminal; resolving the item's LIVE
judgment here is also what keeps a terminal decision pointed at the same
judgment the console and the driver are looking at.

These commands add no judgment of their own. Authority, base revalidation, the
chained audit event and the tracker write all live in ``kew.service.verbs``,
exactly as they do for the web routes; this module resolves the ledger and the
judgment, calls the verb, and turns its refusals into the documented exit codes.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer

from kew import cli as _c
from kew.cli._apps import no_change_app
from kew.ids import InvalidItemIdError, item_ref, provider_from_config
from kew.no_change import NoChangeError
from kew.progress import _error, _success
from kew.service.verbs import (
    NoChangeNotFound,
    OutcomeStale,
    RejudgeUnaudited,
    VerbDenied,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from kew.config import DispatchConfig
    from kew.run_store import RunStore

#: Every refusal these verbs raise. They are one exit code on purpose: each one
#: means the decision was NOT recorded, and a script branching on the outcome
#: needs that distinction, not a taxonomy of why. The one exception to "not
#: recorded" is :class:`RejudgeUnaudited`: the base moved and its audit row did
#: not land, and its message says exactly that.
_REFUSALS = (
    VerbDenied,
    NoChangeNotFound,
    OutcomeStale,
    NoChangeError,
    ValueError,
    RejudgeUnaudited,
)


def _ledger(command: str) -> tuple[Path, DispatchConfig]:
    """The project's ledger path and config, or a refusal naming what is missing.

    The verbs read the forge (the authenticated login, the actor's repository
    permission, the base sha), so the tool allowlist is resolved here —
    without it ``run`` has no absolute path for ``gh`` and raises.
    """
    _c.preflight_tools()
    config = _c.load_config()
    db_path = Path(config.logging.dir).resolve() / "dispatch.db"
    if not db_path.is_file():
        _error(f"kew no-change {command}: no ledger at {db_path}")
        raise typer.Exit(2)
    return db_path, config


def _live_judgment(
    store: RunStore, config: DispatchConfig, *, command: str, item: str
) -> str:
    """The judgment that currently owns this item's outcome, or a refusal.

    Only a LIVE judgment resolves — one that is pending a person, or decided
    and still owing its source a write. A completed or rejected judgment owes
    nothing and hands the item back to the ordinary dimensions, so "nothing is
    held on an absence here" is the honest answer rather than a second decision
    about one somebody already made.
    """
    provider = provider_from_config(config)
    try:
        canonical = _c.validate_item_id(item, provider)
    except InvalidItemIdError as exc:
        _error(f"kew no-change {command}: {exc}")
        raise typer.Exit(2) from None
    reader = getattr(store, "live_no_change_judgment", None)
    judgment = reader(f"{provider}:{canonical}") if callable(reader) else None
    if judgment is None:
        _error(
            f"kew no-change {command}: {item_ref(canonical)} has no live"
            " no-change outcome — nothing is held on an absence for this item"
        )
        raise typer.Exit(2)
    return str(judgment.judgment_id)


def _decide(
    command: str,
    item: str,
    verb: Callable[[RunStore, DispatchConfig, str], dict[str, Any]],
) -> tuple[str, dict[str, Any]]:
    """Run one no-change verb against this item's live judgment, or exit 2.

    The refusal text is the verb's own: it already names the forge login the
    permission was read for and the rule that was in force, which is the whole
    diagnosis an operator needs.
    """
    db_path, config = _ledger(command)
    with _c._open_run_store(str(db_path)) as store:
        config = _c.load_config(store=store)
        _c._detect_repo(config)
        judgment_id = _live_judgment(store, config, command=command, item=item)
        try:
            return judgment_id, verb(store, config, judgment_id)
        except _REFUSALS as exc:
            _error(f"kew no-change {command}: {exc}")
            raise typer.Exit(2) from None


def _echo_sync(result: dict[str, Any]) -> None:
    """Print what the synchronization did to each target it was given."""
    for row in result.get("sync", ()):
        typer.echo(
            f"  {row['target_kind']}:{row['target_ref']}"
            f"  state={row['state']}"
            f"  observed={row['observation']}"
            + (f"  {row['detail']}" if row["detail"] else "")
        )


_ITEM = Annotated[
    str,
    typer.Argument(
        metavar="ITEM",
        help="Work item: a GitHub issue number (2445) or a tracker id (SFD-12).",
    ),
]


@no_change_app.command("confirm")
def no_change_confirm(item: _ITEM) -> None:
    """Authorize this item's no-change outcome and synchronize it to its source.

    Confirming closes the source item on the strength of an absence, so the
    verb revalidates the base the judgment was made against first: a no-change
    is a claim about a commit, and the claim expires when the commit does.
    """
    judgment_id, result = _decide(
        "confirm",
        item,
        lambda store, config, judgment_id: _c.service_verbs.confirm_no_change(
            store, judgment_id=judgment_id, config=config, source="cli"
        ),
    )
    _success(f"Confirmed {judgment_id} — state={result['state']}.")
    _echo_sync(result)


@no_change_app.command("reject")
def no_change_reject(
    item: _ITEM,
    reason: Annotated[
        str,
        typer.Option("--reason", help="Why the absence was not the answer. Required."),
    ],
) -> None:
    """Refuse this item's no-change outcome. Closes nothing, unblocks nothing.

    The item is left open and unsatisfied on purpose: somebody decided the
    absence was not the answer, and what to do instead is their next decision,
    not this command's. The reason is recorded and posted to the item.
    """
    judgment_id, result = _decide(
        "reject",
        item,
        lambda store, config, judgment_id: _c.service_verbs.reject_no_change(
            store,
            judgment_id=judgment_id,
            reason=reason,
            config=config,
            source="cli",
        ),
    )
    noted = "noted on the item" if result["disposition_noted"] else "NOT noted remotely"
    _success(f"Rejected {judgment_id} — state={result['state']}, {noted}.")


def _touched(result: dict[str, Any]) -> str:
    """The paths a rejudge found touched, for its output line."""
    touched = result.get("touched_paths")
    if touched is None:
        return "unknown (the commits since could not be compared)"
    return ", ".join(touched) or "none"


@no_change_app.command("rejudge")
def no_change_rejudge(
    item: _ITEM,
    accept_changes: Annotated[
        bool,
        typer.Option(
            "--accept-changes",
            help="Record the new base even though the commits since touched"
            " what the outcome is about, or could not be compared. Needs --reason.",
        ),
    ] = False,
    reason: Annotated[
        str,
        typer.Option("--reason", help="Why the changes leave the no-change standing."),
    ] = "",
) -> None:
    """Re-check this item's no-change against the current base, and record it.

    The way forward when a confirmation was refused as stale. No agent is
    dispatched and no run is recorded; the outcome stays pending, so confirming
    it is still the next decision. Refuses, recording nothing, when the commits
    since touched a judged path, main does not descend from the judged base, or
    the comparison could not run — unless --accept-changes --reason is passed.
    """
    judgment_id, result = _decide(
        "rejudge",
        item,
        lambda store, config, judgment_id: _c.service_verbs.rejudge_no_change(
            store,
            judgment_id=judgment_id,
            config=config,
            accept_changes=accept_changes,
            reason=reason,
            source="cli",
        ),
    )
    previous = result.get("previous_base_sha")
    moved = f" (was {previous})" if previous else " (unchanged)"
    _success(
        f"Rejudged {judgment_id} on base {result['base_sha']}{moved} —"
        f" state={result['state']}. Next: kew no-change confirm {item}."
    )
    if previous:
        typer.echo(f"  touched since the judged base: {_touched(result)}")
    if result.get("accepted"):
        typer.echo(f"  accepted: {result['accepted']} — {reason}")


@no_change_app.command("retry-sync")
def no_change_retry_sync(item: _ITEM) -> None:
    """Re-run a synchronization that held, for an outcome already decided.

    Safe to press repeatedly: the reconciler looks for this judgment's own
    marker on the item before writing anything.
    """
    judgment_id, result = _decide(
        "retry-sync",
        item,
        lambda store, config, judgment_id: _c.service_verbs.retry_no_change_sync(
            store, judgment_id=judgment_id, config=config, source="cli"
        ),
    )
    _success(f"Retried {judgment_id} — state={result['state']}.")
    _echo_sync(result)
