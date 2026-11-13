"""``kew merge``: the merge verdict rule and the project's merge queue.

``check`` (#3196) applies the one merge verdict rule (:mod:`kew.merge_verdict`)
to a PR head an operator is about to merge by hand: it refuses while the newest
real review verdict on that exact head is NEEDS_CHANGES or NEEDS_FULL, unless
an attested ``--override-reason`` names why.

``kew merge queue`` is the project's merge queue (#2814).

``add`` puts a READY PR head in the one FIFO every session on the host shares,
``list`` shows it, ``remove`` takes a PR out, and ``run`` drives one pass of it
here — what ``kew drive`` does every round. The queue lives in the ledger
(:mod:`kew.project_merge_queue`), so it outlives the session that filled it.

``kew merge claim`` takes, renews and releases the merge claim ``kew verify
--pr`` holds by itself (#3198), for a hand flow that verifies a head some
other way: while the claim is live the drive pushes nothing onto that head.
``renew`` and ``release`` name the owner explicitly (``--owner`` or
``KEW_SESSION``): the ``user:pid`` fallback differs on every call.
"""

from __future__ import annotations

import getpass
import os
import shlex
import sqlite3
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer

from kew import cli as _c
from kew.audit import Actor, resolve_actor
from kew.cli._apps import merge_app, merge_claim_app, merge_queue_app
from kew.merge_verdict import (
    OVERRIDABLE_REFUSALS,
    REFUSED_UNKNOWN_VERDICT,
    REFUSED_UNREADABLE_LEDGER,
    VerdictCheck,
    check_merge_verdict,
    record_override,
)
from kew.progress import _error, _success
from kew.project_merge_queue import (
    GhForge,
    QueueError,
    QueueRunner,
    default_session,
    dequeue,
    enqueue,
)


def _operator() -> Actor:
    try:
        login = getpass.getuser()
    except (KeyError, OSError):
        login = "unknown"
    return Actor(type="human", id=login)


def _setup() -> tuple[Any, str, Path]:
    config = _c.load_config()
    repo = _c._epic_repo(config)
    db_path = Path(config.logging.dir).resolve() / "dispatch.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return config, repo, db_path


# A refusal about the verdict itself is a finding (exit 1); any other refusal
# means the check could not run (exit 2), per the exit-code scheme.
_VERDICT_REFUSALS = OVERRIDABLE_REFUSALS | {REFUSED_UNKNOWN_VERDICT}


def _refuse(pr: int, refusal: str, detail: str) -> NoReturn:
    _error(f"kew merge check: PR #{pr} refused ({refusal}): {detail}.")
    raise typer.Exit(1 if refusal in _VERDICT_REFUSALS else 2)


def _judge(
    db_path: Path, pr: int, head: str, reason: str, repo: str
) -> tuple[VerdictCheck, int | None]:
    """The rule's answer for *head*, and the override event it took, if any.

    A ledger that does not exist holds no verdict an operator could rely on,
    so the check refuses rather than creating one.
    """
    if not db_path.is_file():
        _refuse(pr, REFUSED_UNREADABLE_LEDGER, f"no ledger at {db_path}")
    try:
        with _c._open_run_store(str(db_path)) as store:
            result = check_merge_verdict(store, head)
            if result.refusal not in OVERRIDABLE_REFUSALS or not reason:
                return result, None
            return result, record_override(
                store, result, reason=reason, actor=resolve_actor(), pr=pr, repo=repo
            )
    except typer.Exit:
        raise
    except (sqlite3.Error, OSError, RuntimeError) as exc:
        _refuse(pr, REFUSED_UNREADABLE_LEDGER, f"the ledger could not be used: {exc}")


@merge_app.command("check")
def check(
    pr: Annotated[int, typer.Argument(help="The pull request number.")],
    head: Annotated[
        str, typer.Argument(help="The head commit to merge, all 40 hex digits.")
    ],
    override_reason: Annotated[
        str | None,
        typer.Option(
            "--override-reason",
            help=(
                "Merge over a NEEDS_CHANGES or NEEDS_FULL verdict; attested with "
                "your login, or KEW_ACTOR when set."
            ),
        ),
    ] = None,
) -> None:
    """Refuse a hand merge while the head's newest review verdict stands against it."""
    reason = (override_reason or "").strip()
    if override_reason is not None and not reason:
        _error("kew merge check: --override-reason must name a reason.")
        raise typer.Exit(2)
    _config, repo, db_path = _setup()
    result, override = _judge(db_path, pr, head.strip(), reason, repo)
    if override is not None:
        _success(
            f"PR #{pr} at {result.head[:12]}: {result.detail}; overridden "
            f"(merge_verdict_override event #{override}): {reason}"
        )
        return
    if result.refusal is not None:
        _refuse(pr, result.refusal, result.detail)
    typer.echo(f"PR #{pr} at {result.head[:12]}: {result.detail}.")


@merge_queue_app.command("add")
def add(
    pr: Annotated[int, typer.Argument(help="The pull request number.")],
    head: Annotated[
        str, typer.Option("--head", help="The READY head commit, all 40 hex digits.")
    ],
    session: Annotated[
        str | None,
        typer.Option(help="Who is queueing; defaults to KEW_SESSION or user:pid."),
    ] = None,
) -> None:
    """Queue a READY PR head behind every entry already queued."""
    config, repo, db_path = _setup()
    with _c._open_run_store(str(db_path)) as store:
        try:
            entry, created = enqueue(
                store,
                GhForge(repo),
                pr=pr,
                head=head,
                session=session or default_session(),
                actor=_operator(),
                base=config.branches.base or "main",
            )
        except QueueError as exc:
            _error(f"kew merge queue add: {exc}")
            raise typer.Exit(1) from None
        position = [e.id for e in store.list_merge_queue(repo)].index(entry.id) + 1
    verb = "Queued" if created else "Already queued:"
    _success(f"{verb} PR #{pr} at {entry.head[:12]}, position {position}.")


@merge_queue_app.command("list")
def list_queue(
    all_entries: Annotated[
        bool, typer.Option("--all", help="Include merged, failed and dropped entries.")
    ] = False,
) -> None:
    """Show the queue in the order it merges."""
    _config, repo, db_path = _setup()
    with _c._open_run_store(str(db_path)) as store:
        entries = store.list_merge_queue(repo, include_settled=all_entries)
        stops = [s for s in store.list_merge_queue_suspects() if s.repo == repo]
    for stop in stops:
        typer.echo(
            f"The queue is stopped: {stop.state()} after {stop.reason}. Repair "
            "main, then resume with `kew merge queue run`."
        )
    if not entries:
        typer.echo("The merge queue is empty.")
        return
    for entry in entries:
        line = f"#{entry.pr}  {entry.head[:12]}  {entry.state:<8}  {entry.session}"
        if entry.owner:
            line += f"  (claimed by {entry.owner})"
        if entry.reason:
            line += f"  — {entry.reason}"
        typer.echo(line)


@merge_queue_app.command("remove")
def remove(pr: Annotated[int, typer.Argument(help="The pull request number.")]) -> None:
    """Take a PR out of the queue."""
    _config, repo, db_path = _setup()
    with _c._open_run_store(str(db_path)) as store:
        try:
            entry = dequeue(store, repo, pr=pr, actor=_operator())
        except QueueError as exc:
            _error(f"kew merge queue remove: {exc}")
            raise typer.Exit(1) from None
    if entry is None:
        _error(f"kew merge queue remove: PR #{pr} is not queued.")
        raise typer.Exit(1)
    _success(f"Removed PR #{pr} ({entry.head[:12]}) from the merge queue.")


@merge_queue_app.command("run")
def run_queue() -> None:
    """Verify and merge the head of the queue once, as the drive does."""
    from kew.config import project_root
    from kew.policy import repo_root

    config, repo, db_path = _setup()
    with _c._open_run_store(str(db_path)) as store:
        runner = QueueRunner(
            config,
            store,
            repo=repo,
            # The project's repository, as the drive's merge pass uses (#3108).
            repo_root=repo_root(project_root()),
            base=config.branches.base or "main",
            say=typer.echo,
        )
        done = runner.run_once()
    if done.held:
        _error(f"kew merge queue run: nothing settled — {done.held}")
        raise typer.Exit(2)
    if not done.settled and not done.requeued:
        typer.echo("The merge queue is empty.")


_CLAIM_HEAD = Annotated[
    str, typer.Option("--head", help="The PR head commit, all 40 hex digits.")
]
_CLAIM_OWNER = Annotated[
    str | None,
    typer.Option(
        "--owner",
        help="Who holds the claim; defaults to KEW_SESSION or user:pid.",
    ),
]
_HELD_OWNER = Annotated[
    str | None,
    typer.Option(
        "--owner",
        help="The owner take printed; required unless KEW_SESSION is set.",
    ),
]


def _held_owner(owner: str | None, command: str) -> str:
    """The owner *command* acts for: ``--owner``, else ``KEW_SESSION``.

    Never ``take``'s ``user:pid`` fallback: that pid is the shell's (or
    ``uv run``'s) and differs on every call, so a renew or release under it
    would name somebody else's claim and fail.
    """
    holder = owner or os.environ.get("KEW_SESSION", "").strip()
    if not holder:
        _error(
            f"kew merge claim {command}: pass --owner with the owner `take` "
            "printed, or set KEW_SESSION to the name `take` ran under"
        )
        raise typer.Exit(2)
    return holder


def _claim_store() -> Any:
    _config, _repo, db_path = _setup()
    return _c._open_run_store(str(db_path))


def _full_head(head: str) -> str:
    if len(head) != 40 or any(c not in "0123456789abcdef" for c in head.lower()):
        _error(f"kew merge claim: --head must be all 40 hex digits, not {head!r}")
        raise typer.Exit(2)
    return head.lower()


@merge_claim_app.command("take")
def claim_take(
    pr: Annotated[int, typer.Argument(help="The pull request number.")],
    head: _CLAIM_HEAD,
    owner: _CLAIM_OWNER = None,
) -> None:
    """Claim a PR head for 15 minutes; renew it every 5 while you verify."""
    from kew.merge_claim import utc_now
    from kew.run_store_merge_claims import MergeClaimRefused

    holder = owner or default_session()
    with _claim_store() as store:
        try:
            (claim,) = store.take_merge_claim(
                pr, (_full_head(head),), owner=holder, now=utc_now()
            )
        except MergeClaimRefused as exc:
            _error(f"kew merge claim take: {exc}")
            raise typer.Exit(1) from None
    _success(
        f"Claimed PR #{pr} at {claim.head[:12]} for {holder} until "
        f"{claim.expires_at.isoformat(timespec='seconds')}."
    )
    typer.echo(f"Renew and release it with --owner {shlex.quote(holder)}.")


@merge_claim_app.command("renew")
def claim_renew(
    pr: Annotated[int, typer.Argument(help="The pull request number.")],
    head: _CLAIM_HEAD,
    owner: _HELD_OWNER = None,
) -> None:
    """Extend your live claim by another 15 minutes."""
    from kew.merge_claim import utc_now

    holder = _held_owner(owner, "renew")
    full = _full_head(head)
    with _claim_store() as store:
        renewed = store.renew_merge_claim(pr, (full,), owner=holder, now=utc_now())
        claim = store.live_merge_claim(pr, full, now=utc_now()) if renewed else None
    if claim is None:
        _error(
            f"kew merge claim renew: {holder} holds no live claim on PR #{pr} at "
            f"{full[:12]}; it lapsed or was never taken — take it again"
        )
        raise typer.Exit(1)
    _success(
        f"Renewed PR #{pr} at {full[:12]} until "
        f"{claim.expires_at.isoformat(timespec='seconds')}."
    )


@merge_claim_app.command("release")
def claim_release(
    pr: Annotated[int, typer.Argument(help="The pull request number.")],
    head: _CLAIM_HEAD,
    owner: _HELD_OWNER = None,
) -> None:
    """Give the head back to the drive."""
    holder = _held_owner(owner, "release")
    full = _full_head(head)
    with _claim_store() as store:
        released = store.release_merge_claim(pr, (full,), owner=holder)
    if not released:
        _error(
            f"kew merge claim release: {holder} holds no claim on PR #{pr} "
            f"at {full[:12]}."
        )
        raise typer.Exit(1)
    _success(f"Released PR #{pr} at {full[:12]}.")


@merge_claim_app.command("list")
def claim_list() -> None:
    """Show every live merge claim."""
    from kew.merge_claim import utc_now

    now = utc_now()
    with _claim_store() as store:
        claims = [c for c in store.list_merge_claims() if c.live(now)]
    if not claims:
        typer.echo("No PR head is claimed.")
        return
    for claim in claims:
        typer.echo(
            f"#{claim.pr}  {claim.head[:12]}  {claim.owner}  until "
            f"{claim.expires_at.isoformat(timespec='seconds')}"
        )
