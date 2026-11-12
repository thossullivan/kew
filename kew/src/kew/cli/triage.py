"""`kew triage` command group: a classifier proposes, a person decides (#2293).

Three commands, and only one of them acts:

* ``kew triage <item>`` classifies an item and records the proposal. It applies
  nothing to the tracker — no label, no comment, no ``ready``, no close — even
  when the proposal is ``propose_dispatch``.
* ``kew triage queue`` prints the proposals nobody has decided yet, read-only.
* ``kew triage decide <item> --dispatch | --ask | --close-duplicate N |
  --decline`` records a PERSON's decision, with their name, as
  ``triage_resolved`` on the audit chain. This is the only command here that
  changes a proposal's standing, and it records WHO — the station cannot
  resolve its own proposal because there is no code path that supplies an
  actor for it.

``[triage] enabled`` gates the classifying command: an operator who has not
turned the station on gets a refusal rather than a surprise model call.
``queue`` and ``decide`` are not gated — proposals already on the ledger stay
readable and resolvable after the station is switched off, which is the only
way an operator can drain a queue they no longer want to grow.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer

from kew import cli as _c
from kew.cli._apps import triage_app
from kew.progress import _error, _success, _warn
from kew.run_store_triage import TRIAGE_RESOLUTIONS
from kew.subprocess_utils import run
from kew.triage.station import epic_membership, resolve, triage_item

if TYPE_CHECKING:
    from kew.config import DispatchConfig
    from kew.run_store import TriageDecisionRow

#: Exit 1 — "it ran, and here is a finding". A recorded ``unavailable``
#: proposal is exactly that: the station worked, the classifier did not, and a
#: person now has a row to look at.
UNAVAILABLE_EXIT_CODE = 1


@triage_app.command("item", hidden=True)
def triage_one(
    item: Annotated[
        str,
        typer.Argument(help="Work item: a GitHub issue number (2293)"),
    ],
) -> None:
    """Classify one item and record the proposal (``kew triage <item>``).

    Registered hidden under the name ``item`` because an operator types
    ``kew triage 2293``; ``kew.cli._apps._TriageGroup`` routes the bare form
    here so ``queue`` and ``decide`` stay real subcommands.
    """
    config = _c.load_config()
    if not config.triage.enabled:
        _error(
            "kew triage: the triage station is off."
            " Set [triage] enabled = true in kew.toml to turn it on."
        )
        raise typer.Exit(2)
    issue_num = _issue_number(item)
    db_path = _db_path(config)
    with _c._open_run_store(str(db_path)) as store:
        # The skip is decided before the item is read: an epic node needs
        # neither the issue read nor the classifier call.
        if _skip_as_epic_node(store, item, config):
            return
        fetched = _read_issue(issue_num, config)
        result = triage_item(
            store,
            item_id=item,
            title=fetched["title"],
            body=fetched["body"],
            config=config,
            issue_num=issue_num,
            labels=fetched["labels"],
            source_tracker=config.tracker.type,
            repo=config.project.repo,
        )
    _echo_outcome(item, result)
    if result.row is None:
        _error(
            f"kew triage: the proposal for {item} could not be recorded;"
            " nobody will be shown it"
        )
        raise typer.Exit(2)
    if result.outcome.kind == "unavailable":
        raise typer.Exit(UNAVAILABLE_EXIT_CODE)


#: The ``schema`` field of ``kew triage queue --json``; bumped on a breaking change.
TRIAGE_QUEUE_SCHEMA_VERSION = 1


@triage_app.command("queue")
def triage_queue(
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print one JSON document instead of text."),
    ] = False,
) -> None:
    """Print every triage proposal no person has decided yet."""
    db_path = _db_path(_c.load_config())
    if not db_path.is_file():
        _error(f"kew triage queue: no ledger at {db_path}")
        raise typer.Exit(2)
    rows = _read_queue(db_path)
    if json_output:
        document = {
            "schema": TRIAGE_QUEUE_SCHEMA_VERSION,
            "proposals": [_row_document(row) for row in rows],
        }
        typer.echo(json.dumps(document, indent=2))
        return
    if not rows:
        typer.echo("kew triage queue: no proposals are waiting on a person")
        return
    for row in rows:
        _echo_row(row)


@triage_app.command("decide")
def triage_decide(
    item: Annotated[
        str, typer.Argument(help="Work item whose proposal you are deciding")
    ],
    dispatch: Annotated[
        bool, typer.Option("--dispatch", help="You will dispatch this item.")
    ] = False,
    ask: Annotated[
        bool, typer.Option("--ask", help="You asked the item's author for more.")
    ] = False,
    close_duplicate: Annotated[
        int | None,
        typer.Option(
            "--close-duplicate",
            metavar="N",
            help="You closed this item as a duplicate of issue N.",
        ),
    ] = None,
    decline: Annotated[
        bool, typer.Option("--decline", help="You declined this item.")
    ] = False,
    by: Annotated[
        str,
        typer.Option(
            "--by",
            help="Who is deciding (recorded on the audit chain; defaults to $USER).",
        ),
    ] = "",
) -> None:
    """Record YOUR decision about an item's triage proposal.

    kew does not perform the decision: this records what you chose, who you
    are, and when. Dispatching the item is still ``kew dispatch``, and closing
    it is still yours to do — a station that could close an issue off its own
    proposal would be the station deciding.
    """
    resolution = _one_resolution(dispatch, ask, close_duplicate, decline)
    actor = _actor(by)
    config = _c.load_config()
    db_path = _db_path(config)
    if not db_path.is_file():
        _error(f"kew triage decide: no ledger at {db_path}")
        raise typer.Exit(2)
    with _c._open_run_store(str(db_path)) as store:
        row = _current_proposal(store, item)
        if row is None:
            _error(
                f"kew triage decide: no unresolved triage proposal for {item}."
                " Run `kew triage queue` to see what is waiting."
            )
            raise typer.Exit(2)
        resolved = resolve(
            store,
            row.id,
            resolution=resolution,
            resolved_by=actor,
            repo=config.project.repo,
        )
    if resolved is None:
        _error(f"kew triage decide: proposal {row.id} for {item} vanished mid-decision")
        raise typer.Exit(2)
    detail = (
        f" (duplicate of #{close_duplicate})" if resolution == "close_duplicate" else ""
    )
    _success(
        f"kew triage decide: {item} resolved as {resolved.resolution}{detail}"
        f" by {resolved.resolved_by}"
    )


def _skip_as_epic_node(store: Any, item: str, config: DispatchConfig) -> bool:
    """Whether ``[triage] skip_epic_nodes`` leaves *item* alone.

    An epic already planned its nodes, so re-triaging one proposes nothing a
    person does not already have. A journal that could NOT be read is not
    treated as "not an epic node": it warns and triages, because a skip is a
    decision and this one would have been taken on no evidence.
    """
    if not config.triage.skip_epic_nodes:
        return False
    membership = epic_membership(store, item)
    if membership.is_node is None:
        _warn(
            "kew triage: could not read the epic journal, so [triage]"
            f" skip_epic_nodes could not be applied to {item}; triaging it"
        )
        return False
    if not membership.is_node:
        return False
    typer.echo(
        f"kew triage: skipped {item} — it is a node of epic"
        f" {membership.epic_ref}, whose plan already covers it"
        " ([triage] skip_epic_nodes)."
    )
    return True


def _one_resolution(
    dispatch: bool, ask: bool, close_duplicate: int | None, decline: bool
) -> str:
    """Exactly one of the four flags, or refuse.

    A decision with no flag and a decision with two flags are both ambiguous,
    and recording either would put a resolution on the chain that nobody chose.
    """
    chosen = [
        name
        for name, given in (
            ("dispatch", dispatch),
            ("ask", ask),
            ("close_duplicate", close_duplicate is not None),
            ("decline", decline),
        )
        if given
    ]
    if len(chosen) != 1:
        _error(
            "kew triage decide: choose exactly one of --dispatch, --ask,"
            " --close-duplicate N, --decline"
            f" (you gave {len(chosen)})"
        )
        raise typer.Exit(2)
    if close_duplicate is not None and close_duplicate < 1:
        _error("kew triage decide: --close-duplicate needs an issue number")
        raise typer.Exit(2)
    resolution = chosen[0]
    assert resolution in TRIAGE_RESOLUTIONS
    return resolution


def _actor(by: str) -> str:
    """Who is deciding. Refuses rather than recording an anonymous decision."""
    import getpass
    import os

    name = by.strip()
    if not name:
        name = (os.environ.get("USER") or "").strip()
    if not name:
        try:
            name = getpass.getuser().strip()
        except Exception as exc:  # noqa: BLE001 — no identity is a refusal, not a default
            _error(
                f"kew triage decide: cannot tell who you are ({exc}); pass --by NAME"
            )
            raise typer.Exit(2) from exc
    if not name:
        _error("kew triage decide: cannot tell who you are; pass --by NAME")
        raise typer.Exit(2)
    return name


def _db_path(config: DispatchConfig) -> Path:
    return Path(config.logging.dir).resolve() / "dispatch.db"


def _issue_number(item: str) -> int:
    """*item* as a GitHub issue number, or refuse.

    The shadow-mode station reads an item's title and body through ``gh``, so a
    non-numeric tracker id has nothing to read. Refusing names the limit rather
    than triaging an empty body.
    """
    try:
        return int(item)
    except ValueError:
        _error(
            f"kew triage: {item!r} is not a GitHub issue number."
            " The triage station reads items through gh in this release."
        )
        raise typer.Exit(2) from None


def _read_issue(issue_num: int, config: DispatchConfig) -> dict[str, Any]:
    """Read the item's title, body and labels. A failed read refuses (exit 2).

    Read-only: ``gh issue view`` cannot label, comment or close. A failure is
    reported as a failure — "we could not read the item" must never reach the
    classifier as an empty body.
    """
    repo = config.project.repo
    if not repo:
        _error("kew triage: no repository configured ([project] repo)")
        raise typer.Exit(2)
    result = run(
        [
            "gh",
            "issue",
            "view",
            str(issue_num),
            "--repo",
            repo,
            "--json",
            "number,title,body,labels",
        ],
        check=False,
    )
    if result.returncode != 0:
        _error(
            f"kew triage: could not read issue #{issue_num} from {repo}:"
            f" {(result.stderr or '').strip()[:200]}"
        )
        raise typer.Exit(2)
    try:
        data = json.loads(result.stdout or "{}")
    except (TypeError, ValueError) as exc:
        _error(f"kew triage: unreadable issue payload for #{issue_num}: {exc}")
        raise typer.Exit(2) from exc
    if not isinstance(data, dict):
        _error(f"kew triage: unreadable issue payload for #{issue_num}")
        raise typer.Exit(2)
    labels = tuple(
        str(label.get("name", ""))
        for label in data.get("labels", [])
        if isinstance(label, dict)
    )
    return {
        "title": str(data.get("title") or ""),
        "body": data.get("body"),
        "labels": labels,
    }


def _read_queue(db_path: Path) -> list[TriageDecisionRow]:
    """The unresolved proposals, read-only. A ledger that cannot serve them refuses."""
    try:
        with _c._open_run_store(str(db_path), read_only=True) as store:
            return list(store.list_unresolved_triage_decisions())
    except sqlite3.DatabaseError as exc:
        _error(
            f"kew triage queue: cannot read proposals from {db_path}: {exc}\n"
            "       A ledger older than the triage station migrates on the next"
            " kew command that writes it."
        )
        raise typer.Exit(2) from exc


def _current_proposal(store: Any, item: str) -> TriageDecisionRow | None:
    """*item*'s current proposal, when a person still has to decide it."""
    row: TriageDecisionRow | None = store.current_triage_decision(item)
    if row is None or not row.unresolved:
        return None
    return row


def _echo_outcome(item: str, result: Any) -> None:
    """Print what triage proposed, and that it is a proposal."""
    outcome = result.outcome
    typer.echo(f"{item}: {outcome.kind} — {outcome.reason}")
    for question in outcome.questions:
        typer.echo(f"  ? {question}")
    if outcome.kind == "unavailable":
        _warn(f"kew triage: {item} needs a person; nothing was proposed")
        return
    if not result.recorded:
        typer.echo("  (unchanged body — the existing proposal still stands)")
    _success(f"kew triage: recorded a proposal for {item}. Nothing was applied.")
    typer.echo(
        f"  decide it with: kew triage decide {item} --dispatch | --ask"
        " | --close-duplicate N | --decline"
    )


def _row_document(row: TriageDecisionRow) -> dict[str, Any]:
    """One queue row for ``--json``: the fields the text line prints, and more."""
    return {
        "item_id": row.item_id,
        "issue_num": row.issue_num,
        "outcome": row.outcome,
        "type": row.type,
        "priority": row.priority,
        "complexity": row.complexity,
        "confidence": row.confidence,
        "actionable": row.actionable,
        "needs_info": row.needs_info,
        "duplicate_of": row.duplicate_of,
        "backend": row.backend,
        "model": row.model,
        "recorded_at": row.recorded_at.isoformat(),
        "decide": f"kew triage decide {row.item_id}",
    }


def _echo_row(row: TriageDecisionRow) -> None:
    """One queue line: the item, the outcome, and the command that decides it."""
    typer.echo(
        f"{row.item_id}: {row.outcome}"
        f"  type={row.type or '-'}"
        f"  confidence={'-' if row.confidence is None else f'{row.confidence:.2f}'}"
        f"  recorded={row.recorded_at.isoformat()}"
    )
    typer.echo(f"  decide it with: kew triage decide {row.item_id}")
