"""`kew prompts` command group: see and change each role's prompt profile (#3021).

A profile is an operator addendum to a role's packaged prompt, never a
replacement. This module adds no judgment of its own: the approver gate, the
generation check and the chained audit event all live in
:mod:`kew.service.prompt_profiles`, exactly as they do for the console. It
reads the ledger, calls the service, and turns each refusal into exit 2.

``approve`` and ``retire`` take the role generation the person SAW, from
``kew prompts list`` or ``kew prompts show``. It is never read again here just
before the call: a generation re-read at decision time would always match, and
the check would prove nothing.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, NoReturn

import typer

from kew import cli as _c
from kew.audit import DEFAULT_ANCHORS_PATH
from kew.cli._apps import prompts_app
from kew.config import project_root
from kew.ledger_identity import (
    LedgerIdentityError,
    canonical_project_root,
    ledger_never_created,
    lost_ledger_reason,
)
from kew.policy import repo_root as _git_repo_root
from kew.progress import _error, _success
from kew.service import prompt_profiles as profiles
from kew.service.prompts import (
    ROLES,
    PreviewUnavailable,
    PromptPreviewDTO,
    PromptRoleDTO,
    PromptSourceUnreadable,
    UnknownPromptRole,
    list_roles,
    role_preview,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from kew.audit import Actor
    from kew.config import DispatchConfig
    from kew.run_store import SqliteRunStore
    from kew.run_store_prompt_profiles import PromptProfileVersion

#: Every refusal a decision raises. Each means nothing was written.
_REFUSALS = (
    profiles.OverrideDenied,
    profiles.StaleGeneration,
    profiles.PromptProfileRefused,
    profiles.PromptProfileNotFound,
    profiles.PromptProfileConflict,
)

_ROLE = Annotated[
    str, typer.Argument(metavar="ROLE", help="Agent role, e.g. dispatch.")
]
_VERSION = Annotated[int, typer.Argument(metavar="VERSION", help="Profile version.")]
_GENERATION = Annotated[
    int,
    typer.Option(
        "--generation",
        help="The role generation you saw in `kew prompts list` or `show`.",
    ),
]
_JSON = Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON.")]


def _db_path(config: DispatchConfig) -> Path:
    return Path(config.logging.dir).resolve() / "dispatch.db"


#: What classifying the project's ledger may raise: a git layout the ledger
#: identity cannot resolve, or a state directory that cannot be searched.
_LAYOUT_ERRORS = (LedgerIdentityError, OSError)


def _never_created(db_path: Path) -> bool:
    """No ledger was ever created: no database, registry or anchor history."""
    root = canonical_project_root(project_root())
    return ledger_never_created(root, db_path, root / DEFAULT_ANCHORS_PATH)


def _classify_for_decision(command: str, db_path: Path, json_output: bool) -> bool:
    """True when no ledger was ever created; refuse (exit 2) a lost one.

    A lost ledger is refused here, not by the open, only when the open would
    refuse it as REGISTERED-LEDGER-MISSING too; a registry recorded for
    another path is left to the open's WRONG-LEDGER refusal. A layout the
    classifier cannot resolve is refused with its own reason.
    """
    try:
        root = canonical_project_root(project_root())
        anchors = root / DEFAULT_ANCHORS_PATH
        if ledger_never_created(root, db_path, anchors):
            return True
        lost = lost_ledger_reason(root, db_path, anchors)
    except _LAYOUT_ERRORS as exc:
        _refuse(command, str(exc), json_output)
    if lost is not None:
        _refuse(command, lost, json_output)
    return False


def _refuse(command: str, reason: str, json_output: bool) -> NoReturn:
    """Name the refusal and exit 2; in JSON mode stdout carries it too."""
    _error(f"kew prompts {command}: {reason}")
    if json_output:
        typer.echo(json.dumps({"outcome": "refused", "reason": reason}))
    raise typer.Exit(2)


#: Reads of one role's profile that a concurrent decision may interrupt.
_SHOW_READ_ATTEMPTS = 3


class _ProfileChangedDuringRead(Exception):
    """A profile decision landed between the reads of a role, every time."""


def _profile_state(store: SqliteRunStore | None, role: str) -> dict[str, Any]:
    """*role*'s generation, active version and proposals; empty without a ledger.

    One generation's view: the generation is read before and after the rows,
    and the read is retried when a decision landed in between, so the active
    version and the generation never come from two different decisions.
    """
    if store is None:
        return {"generation": 0, "active": None, "proposed": []}
    for _ in range(_SHOW_READ_ATTEMPTS):
        generation = profiles.current_generation(store, role)
        rows = store.list_prompt_profile_versions(role)
        active = profiles.active(store, role)
        if profiles.current_generation(store, role) == generation:
            return {
                "generation": generation,
                "active": active,
                "proposed": [row for row in rows if row.state == "proposed"],
            }
    raise _ProfileChangedDuringRead(role)


def _read(reader: Callable[[SqliteRunStore | None], Any]) -> Any:
    """Run *reader* over the ledger read-only, or over None when there is none."""
    db_path = _db_path(_c.load_config())
    # No ledger was ever created: nothing is approved. A ledger whose file is
    # gone while its registry or anchors survive is not that; the open below
    # refuses it, as a launch does, instead of reading it as "no profile".
    try:
        never_created = _never_created(db_path)
    except _LAYOUT_ERRORS as exc:
        _error(f"kew prompts: cannot locate the ledger for {db_path}: {exc}")
        raise typer.Exit(2) from None
    if never_created:
        return reader(None)
    try:
        with _c._open_run_store(str(db_path), read_only=True) as store:
            return reader(store)
    except sqlite3.DatabaseError as exc:
        # A ledger older than prompt profiles migrates on its next write; a
        # read-only open cannot, so say so instead of a traceback.
        _error(f"kew prompts: cannot read profiles from {db_path}: {exc}")
        raise typer.Exit(2) from None


def _decide(
    command: str,
    json_output: bool,
    decision: Callable[[SqliteRunStore, DispatchConfig, Actor], PromptProfileVersion],
    *,
    precheck: Callable[[], None],
    needs_ledger: bool = True,
) -> PromptProfileVersion:
    """Run one service decision against the ledger, or exit 2 on a refusal.

    A SQLite failure of the open or the decision is a refusal too.

    The actor and the input are checked before the ledger is opened, because
    the open is create-capable: a refusal leaves the filesystem unchanged
    (#3080). The service checks both again against the opened store. Only a
    proposal may create the ledger; every other decision acts on a version
    that one recorded.
    """
    config = _c.load_config()
    db_path = _db_path(config)
    try:
        # [approval].approvers is not a DB-overridable field, so the file
        # config's gate is the one the service applies after the open.
        actor = profiles.authorize(config)
        precheck()
    except _REFUSALS as exc:
        _refuse(command, str(exc), json_output)
    if _classify_for_decision(command, db_path, json_output) and needs_ledger:
        _refuse(
            command,
            f"no ledger at {db_path}; no prompt profile has been proposed",
            json_output,
        )
    try:
        with _c._open_run_store(str(db_path)) as store:
            config = _c.load_config(store=store)
            try:
                return decision(store, config, actor)
            except _REFUSALS as exc:
                _refuse(command, str(exc), json_output)
    except sqlite3.DatabaseError as exc:
        # A lock held past the retry budget, a read-only file or a failed
        # write: the service writes each decision in one transaction, so
        # nothing was applied (#3079).
        _refuse(command, f"cannot write {db_path}: {exc}", json_output)


def _report(row: PromptProfileVersion, json_output: bool) -> None:
    if json_output:
        typer.echo(
            json.dumps(
                {
                    "outcome": "success",
                    "role": row.role,
                    "version": row.version,
                    "state": row.state,
                    "generation": row.generation,
                    "body_sha256": row.body_sha256,
                }
            )
        )
        return
    _success(f"{row.role} profile v{row.version} {row.state}")


def _list_row(described: PromptRoleDTO, state: dict[str, Any] | None) -> dict[str, Any]:
    """One role's line of ``kew prompts list``; *state* is None without profiles."""
    row: dict[str, Any] = {
        "role": described.role,
        "source": f"{described.source.kind}:{described.source.name}",
        "profile_applies": described.profile_applies,
        "active_version": None,
        "generation": None,
        "proposed_versions": [],
    }
    if state is not None:
        active = state["active"]
        row["active_version"] = None if active is None else active.version
        row["generation"] = state["generation"]
        row["proposed_versions"] = [r.version for r in state["proposed"]]
    return row


def _list_line(row: dict[str, Any]) -> str:
    if not row["profile_applies"]:
        return f"{row['role']}  {row['source']}  profiles do not apply"
    active = row["active_version"]
    proposed = ",".join(f"v{v}" for v in row["proposed_versions"]) or "none"
    return (
        f"{row['role']}  {row['source']}"
        f"  active={'none' if active is None else f'v{active}'}"
        f"  generation={row['generation']}  proposed={proposed}"
    )


def _reviewed_repo_root() -> Path:
    """The root a preview reads deployed prompts from: the top of the
    project's Git repository, the root the drive passes every review launch
    (``kew.graph.driver.tick``) and ``kew serve`` serves, never the project's
    own subdirectory of it."""
    return _git_repo_root(project_root())


@contextmanager
def _unreadable_refused(command: str, json_output: bool) -> Iterator[None]:
    """Refuse *command* when a deployed prompt the base ref lists is unreadable."""
    try:
        yield
    except PromptSourceUnreadable as exc:
        _refuse(
            command,
            f"the prompt {exc.role} runs could not be read from the base ref:"
            f" {exc.reason}",
            json_output,
        )


@prompts_app.command("list")
def prompts_list(json_output: _JSON = False) -> None:
    """List each agent role, its prompt source and its active profile version."""
    try:
        states = _read(
            lambda store: {
                entry.role: _profile_state(store, entry.role)
                for entry in ROLES
                if entry.profile_applies
            }
        )
    except _ProfileChangedDuringRead as exc:
        _refuse(
            "list",
            f"{exc}'s profile kept changing while it was read; run list again",
            json_output,
        )
    with _unreadable_refused("list", json_output):
        described_roles = list_roles(_reviewed_repo_root(), _c.load_config())
    rows = [
        _list_row(described, states.get(described.role))
        for described in described_roles
    ]
    if json_output:
        typer.echo(json.dumps(rows, indent=2))
        return
    for row in rows:
        typer.echo(_list_line(row))


def _show_previews(
    role: str, store: SqliteRunStore | None
) -> tuple[PromptPreviewDTO, PromptPreviewDTO, dict[str, Any]]:
    """*role*'s base preview, its effective preview and its profile state.

    The effective preview is the launch's own composition over the host
    store's approved profile (:func:`role_preview` with the store), so the
    addendum sits where the launch puts it, before the fixed sections, and a
    profile the launch would refuse leaves the base and says why.
    """
    from kew.prompt_previews import PREVIEW_RENDERERS

    # The launch's configuration: the file plus the store's live overrides.
    config = _c.load_config(store=store)
    root = _reviewed_repo_root()
    for _ in range(_SHOW_READ_ATTEMPTS):
        before = _generation(store, role)
        base = role_preview(role, PREVIEW_RENDERERS, config, root)
        effective = role_preview(role, PREVIEW_RENDERERS, config, root, store=store)
        state = _profile_state(store, role)
        # A decision between these reads would pair one version's prompt with
        # another's generation; the generation moves on every decision.
        if state["generation"] == before:
            return base, effective, state
    raise _ProfileChangedDuringRead(role)


def _generation(store: SqliteRunStore | None, role: str) -> int:
    return 0 if store is None else profiles.current_generation(store, role)


@prompts_app.command("show")
def prompts_show(role: _ROLE, json_output: _JSON = False) -> None:
    """Print the prompt a role's launch renders, or why the launch refuses."""
    try:
        with _unreadable_refused("show", json_output):
            base, preview, state = _read(lambda store: _show_previews(role, store))
    except UnknownPromptRole:
        _refuse("show", f"no agent role is named {role!r}", json_output)
    except PreviewUnavailable:
        _refuse("show", f"no prompt preview renders for {role!r}", json_output)
    except _ProfileChangedDuringRead:
        _refuse(
            "show",
            f"{role}'s profile kept changing while it was read; run show again",
            json_output,
        )
    refusal = _launch_refusal(preview)
    if json_output:
        typer.echo(json.dumps(_show_payload(role, base, preview, state), indent=2))
    else:
        _show_text(role, preview, state, printed=refusal is None)
    if refusal is not None:
        _error(
            f"kew prompts show: the {role} launch refuses before it builds"
            f" a prompt: {refusal}"
        )
        raise typer.Exit(2)


def _launch_refusal(preview: PromptPreviewDTO) -> str | None:
    """Why *preview*'s launch builds no prompt, or None when it builds one.

    A composer refusal leaves the base text standing in for the prompt, so
    that text is never the prompt the launch renders (#3071).
    """
    refusal = preview.variant.get("refusal")
    return refusal if isinstance(refusal, str) and refusal else None


def _show_payload(
    role: str,
    base: PromptPreviewDTO,
    preview: PromptPreviewDTO,
    state: dict[str, Any],
) -> dict[str, Any]:
    """``kew prompts show --json``; a refused launch adds its outcome and reason."""
    active = state["active"] if preview.profile_applies else None
    refusal = _launch_refusal(preview)
    payload: dict[str, Any] = (
        {} if refusal is None else {"outcome": "refused", "reason": refusal}
    )
    return payload | {
        "role": role,
        "source": preview.source.model_dump(),
        "profile_applies": preview.profile_applies,
        "generation": state["generation"],
        "active_version": None if active is None else active.version,
        "profile": preview.variant.get("profile", "none"),
        "variant": preview.variant,
        "base_prompt": base.preview,
        "effective_prompt": preview.preview,
        "proposed": [
            {"version": r.version, "body": r.body, "by": r.proposed_by}
            for r in state["proposed"]
        ],
    }


def _show_text(
    role: str, preview: PromptPreviewDTO, state: dict[str, Any], *, printed: bool
) -> None:
    """``kew prompts show``; *printed* is False when the launch builds no prompt."""
    active = state["active"] if preview.profile_applies else None
    version = "none" if active is None else f"v{active.version}"
    profile = preview.variant.get("profile", "none")
    typer.echo(
        f"role: {role}  active={version}  profile={profile}"
        f"  generation={state['generation']}"
    )
    typer.echo("")
    if printed:
        typer.echo(preview.preview)
    for row in state["proposed"]:
        typer.echo(f"--- proposed v{row.version} by {row.proposed_by} ---")
        typer.echo(row.body)


@prompts_app.command("propose")
def prompts_propose(
    role: _ROLE,
    file: Annotated[
        Path,
        typer.Option("--file", help="File holding the addendum's text."),
    ],
    json_output: _JSON = False,
) -> None:
    """Propose an addendum for a role; it changes nothing until approved."""
    try:
        body = file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        _refuse("propose", f"cannot read {file}: {exc}", json_output)
    row = _decide(
        "propose",
        json_output,
        lambda store, config, actor: profiles.propose(
            store, role=role, body=body, config=config, actor=actor
        ),
        precheck=lambda: profiles.check_proposal(role, body),
        needs_ledger=False,
    )
    _report(row, json_output)


@prompts_app.command("approve")
def prompts_approve(
    role: _ROLE, version: _VERSION, generation: _GENERATION, json_output: _JSON = False
) -> None:
    """Activate a proposed profile version, retiring the one it replaces."""
    row = _decide(
        "approve",
        json_output,
        lambda store, config, actor: profiles.approve(
            store,
            role=role,
            version=version,
            generation=generation,
            config=config,
            actor=actor,
        ),
        precheck=lambda: profiles.check_role(role),
    )
    _report(row, json_output)


@prompts_app.command("decline")
def prompts_decline(role: _ROLE, version: _VERSION, json_output: _JSON = False) -> None:
    """Decline a proposed profile version; what the role runs is unchanged."""
    row = _decide(
        "decline",
        json_output,
        lambda store, config, actor: profiles.decline(
            store, role=role, version=version, config=config, actor=actor
        ),
        precheck=lambda: profiles.check_role(role),
    )
    _report(row, json_output)


@prompts_app.command("retire")
def prompts_retire(
    role: _ROLE, generation: _GENERATION, json_output: _JSON = False
) -> None:
    """Retire a role's active profile, leaving it on its base prompt."""
    row = _decide(
        "retire",
        json_output,
        lambda store, config, actor: profiles.retire(
            store, role=role, generation=generation, config=config, actor=actor
        ),
        precheck=lambda: profiles.check_role(role),
    )
    _report(row, json_output)
