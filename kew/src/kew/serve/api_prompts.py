"""Prompt catalog API (#3018) — ``GET /api/prompts``, and profile writes (#3025).

Lists every agent role's prompt source (``kew.service.prompts.ROLES``) and,
per role, a preview of its prompt rendered over a fixed sample item: the base,
plus the role's active approved profile read off the host store as a launch
reads it (#3023). The served project's root (``app.state.repo_dir``) is where
a deployed reviewer copy is looked for, as at review launch. The reads sit
behind ``read_guard``; they read no tracker or checkout.

The operator profile routes (#3025) are thin shims over
:mod:`kew.service.prompt_profiles`: ``GET /{role}/versions`` reads a role's
history and generation, and the four POSTs propose, approve, decline and
retire. Each POST depends on ``require_write`` (session + CSRF + Origin), then
the service's approver gate decides; nothing here derives profile state. An
approval and a retirement carry the generation the person was shown, never one
read again here, so a decision another approver made in between answers 409.
A read serves the history and the generation from one generation's view
(:func:`_versions`), so the generation sent back names the rows that were shown.

``GET /{role}/outcomes`` (#3026) reads what each version of a role's prompt
did — first-pass approval, rounds to READY and merged cost for an implementer,
the verdict mix for the reviewer — off :func:`kew.service.prompt_profiles.outcomes`.

The renderers are ``app.state.prompt_renderers``, injected by ``kew serve``
(:data:`kew.prompt_previews.PREVIEW_RENDERERS`): the builders reach
``kew.graph``, which serve may not import. Without them a preview answers 503.
So do both reads when the base ref lists a deployed reviewer copy that Git
cannot read: the prompt a review would run is then unknown.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, NamedTuple

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from kew.serve.security import read_guard, require_write
from kew.service import prompt_profiles as profiles
from kew.service.prompts import (
    PreviewUnavailable,
    PromptPreviewDTO,
    PromptRoleDTO,
    PromptSourceUnreadable,
    UnknownPromptRole,
    get_role,
    list_roles,
    role_preview,
)

prompts_router = APIRouter(prefix="/api/prompts", tags=["prompts"])


def _unreadable(exc: PromptSourceUnreadable) -> HTTPException:
    """503 for a deployed prompt the base ref lists and Git cannot read."""
    return HTTPException(
        status_code=503,
        detail=(
            f"The prompt {exc.role!r} runs could not be read from the base ref:"
            f" {exc.reason}"
        ),
    )


@prompts_router.get("", dependencies=[Depends(read_guard)])
def get_prompts(request: Request) -> list[PromptRoleDTO]:
    """Every agent role, its run kinds and where its base prompt comes from."""
    try:
        return list_roles(request.app.state.repo_dir, request.app.state.config)
    except PromptSourceUnreadable as exc:
        raise _unreadable(exc) from exc


@prompts_router.get("/{role}", dependencies=[Depends(read_guard)])
def get_prompt(role: str, request: Request) -> PromptPreviewDTO:
    """One role with its prompt rendered over the sample item."""
    state = request.app.state
    renderers = state.prompt_renderers or {}
    try:
        # The host store a launch reads the role's approved profile from.
        with state.store_factory() as store:
            return role_preview(
                role, renderers, state.config, state.repo_dir, store=store
            )
    except UnknownPromptRole as exc:
        raise HTTPException(
            status_code=404,
            detail=f"No agent role is named {exc.role!r}; GET /api/prompts lists them.",
        ) from exc
    except PreviewUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail=(
                f"This server was started without a prompt renderer for {exc.role!r}."
            ),
        ) from exc
    except PromptSourceUnreadable as exc:
        raise _unreadable(exc) from exc


# --- Operator profiles (#3025) ----------------------------------------------


class ProfileVersionDTO(BaseModel):
    """One proposed, active, declined or retired addendum of a role."""

    version: int
    state: str
    body: str
    body_sha256: str
    proposed_by: str
    proposed_at: str
    decided_by: str | None
    decided_at: str | None
    audit_event_id: int | None


class ProfileVersionsDTO(BaseModel):
    """A role's addendum history, newest first, and the generation in force.

    ``generation`` is what an approve or a retire sends back: the role as this
    read showed it. ``can_decide`` says whether the acting human passes the
    approver gate (#3092), so the console offers a write only to someone
    whose write the server would take. ``decide_refusal`` is the gate's own
    sentence while nobody can pass it (no config, or no approver configured),
    the same detail that write's refusal carries, so the console names the
    fix (#3119); None otherwise.
    """

    role: str
    generation: int
    can_decide: bool
    decide_refusal: str | None
    max_body_chars: int
    active_version: int | None
    versions: list[ProfileVersionDTO]


class ProposeBody(BaseModel):
    body: str


class GenerationBody(BaseModel):
    """The role generation the person deciding was shown."""

    generation: int


@contextmanager
def _translated() -> Iterator[None]:
    """Map the profile service's refusals onto HTTP statuses, in one place."""
    try:
        yield
    except UnknownPromptRole as exc:
        raise HTTPException(
            status_code=404, detail=f"No agent role is named {exc.role!r}."
        ) from None
    except profiles.OverrideDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from None
    except (profiles.StaleGeneration, profiles.PromptProfileConflict) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except profiles.PromptProfileNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except profiles.PromptProfileRefused as exc:
        # An approval's refusal too: the console prints a 400's detail, the
        # reason and its remedy, where any other status reads as bare.
        raise HTTPException(status_code=400, detail=str(exc)) from None


#: Reads of one role's history that a concurrent decision may interrupt; the
#: bound ``kew prompts show`` reads under (``kew.cli.prompts``).
_VERSIONS_READ_ATTEMPTS = 3

#: What a read answers when a decision landed inside every attempt: the
#: console prints its 409 sentence, never a history from two generations.
_CHANGED_DURING_READ = (
    "This profile changed while it was being read. Reload to see the new version."
)

#: What a write answers when it committed but the history after it never
#: settled. Never 409: a 409 says nothing was written, and the client would
#: offer to send a committed proposal again. A 5xx the console reads as an
#: unknown outcome and locks every write until a reload.
_SAVED_BUT_UNREAD = (
    "The change was saved, but the new state could not be read. Reload to see it."
)


class _UnsettledHistory(Exception):
    """A decision landed between the generation reads of every attempt."""


class _Viewer(NamedTuple):
    """What the acting human may do: pass the gate, or the server's reason not."""

    can_decide: bool
    decide_refusal: str | None


def _versions(store: Any, role: str, viewer: _Viewer) -> ProfileVersionsDTO:
    """*role*'s history and generation, both from one generation's view.

    The generation is read before and after the rows, and the read is retried
    while a decision lands in between: a history paired with a later
    generation would let an approve or a retire pass the stale-generation
    check against a version the person never saw. A role that never settles
    raises :class:`_UnsettledHistory` rather than answer a split view.
    """
    for _ in range(_VERSIONS_READ_ATTEMPTS):
        generation = store.prompt_profile_generation(role)
        rows = store.list_prompt_profile_versions(role)
        if store.prompt_profile_generation(role) == generation:
            return _versions_dto(role, generation, rows, viewer)
    raise _UnsettledHistory(role)


def _read_versions(store: Any, role: str, request: Request) -> ProfileVersionsDTO:
    """The history a read serves; 409 when it never settles."""
    try:
        return _versions(store, role, _viewer(request))
    except _UnsettledHistory:
        raise HTTPException(status_code=409, detail=_CHANGED_DURING_READ) from None


def _written_versions(store: Any, role: str, request: Request) -> ProfileVersionsDTO:
    """The history a committed write answers with; 503 when it never settles."""
    try:
        return _versions(store, role, _viewer(request))
    except _UnsettledHistory:
        raise HTTPException(status_code=503, detail=_SAVED_BUT_UNREAD) from None


def _viewer(request: Request) -> _Viewer:
    """Whether the acting human passes the approver gate of the served config."""
    config = request.app.state.config
    return _Viewer(profiles.can_decide(config), profiles.decide_refusal(config))


def _versions_dto(
    role: str, generation: int, rows: list[Any], viewer: _Viewer
) -> ProfileVersionsDTO:
    active = next((r.version for r in rows if r.state == "active"), None)
    return ProfileVersionsDTO(
        role=role,
        generation=generation,
        can_decide=viewer.can_decide,
        decide_refusal=viewer.decide_refusal,
        max_body_chars=profiles.MAX_BODY_CHARS,
        active_version=active,
        versions=[
            ProfileVersionDTO(
                version=r.version,
                state=r.state,
                body=r.body,
                body_sha256=r.body_sha256,
                proposed_by=r.proposed_by,
                proposed_at=r.proposed_at,
                decided_by=r.decided_by,
                decided_at=r.decided_at,
                audit_event_id=r.audit_event_id,
            )
            for r in reversed(rows)
        ],
    )


def _config(request: Request) -> Any:
    """The served config the approver gate reads; 503 when there is none.

    Without it there is no ``[approval].approvers`` list to check, so a
    profile write is refused instead.
    """
    config = request.app.state.config
    if config is None:
        raise HTTPException(
            status_code=503,
            detail=profiles.NO_CONFIG,
        )
    return config


def _profile_role(role: str) -> str:
    """*role*, when an operator profile may apply to it; 404 otherwise."""
    if not get_role(role).profile_applies:
        raise HTTPException(
            status_code=404, detail=f"No operator profile applies to {role!r}."
        )
    return role


@prompts_router.get("/{role}/versions", dependencies=[Depends(read_guard)])
def get_profile_versions(role: str, request: Request) -> ProfileVersionsDTO:
    """*role*'s addendum history, newest first, and its generation."""
    with _translated(), request.app.state.store_factory() as store:
        return _read_versions(store, _profile_role(role), request)


@prompts_router.get("/{role}/outcomes", dependencies=[Depends(read_guard)])
def get_profile_outcomes(role: str, request: Request) -> profiles.PromptOutcomesDTO:
    """*role*'s outcomes per prompt version, newest first, built-in last."""
    with _translated(), request.app.state.store_factory() as store:
        return profiles.outcomes(store, _profile_role(role))


@prompts_router.post("/{role}/versions")
def propose_profile(
    role: str,
    body: ProposeBody,
    request: Request,
    _: object = Depends(require_write),
) -> ProfileVersionsDTO:
    """Propose *body* as *role*'s next addendum; nothing runs it until approved."""
    with _translated(), request.app.state.store_factory() as store:
        profiles.propose(
            store,
            role=_profile_role(role),
            body=body.body,
            config=_config(request),
        )
        return _written_versions(store, role, request)


@prompts_router.post("/{role}/versions/{version}/approve")
def approve_profile(
    role: str,
    version: int,
    body: GenerationBody,
    request: Request,
    _: object = Depends(require_write),
) -> ProfileVersionsDTO:
    """Activate *version* against the generation the approver was shown."""
    with _translated(), request.app.state.store_factory() as store:
        profiles.approve(
            store,
            role=_profile_role(role),
            version=version,
            generation=body.generation,
            config=_config(request),
        )
        return _written_versions(store, role, request)


@prompts_router.post("/{role}/versions/{version}/decline")
def decline_profile(
    role: str,
    version: int,
    request: Request,
    _: object = Depends(require_write),
) -> ProfileVersionsDTO:
    """Decline the proposed *version*; what the role runs is unchanged."""
    with _translated(), request.app.state.store_factory() as store:
        profiles.decline(
            store,
            role=_profile_role(role),
            version=version,
            config=_config(request),
        )
        return _written_versions(store, role, request)


@prompts_router.post("/{role}/retire")
def retire_profile(
    role: str,
    body: GenerationBody,
    request: Request,
    _: object = Depends(require_write),
) -> ProfileVersionsDTO:
    """Retire the active addendum against the generation the person was shown."""
    with _translated(), request.app.state.store_factory() as store:
        profiles.retire(
            store,
            role=_profile_role(role),
            generation=body.generation,
            config=_config(request),
        )
        return _written_versions(store, role, request)
