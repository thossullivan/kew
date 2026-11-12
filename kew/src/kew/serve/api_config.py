"""Config-manager write API — GET/POST/DELETE budget overrides (#369).

Reuses the approval-inbox auth spine (require_session / require_write).

The three ``human_review.*`` thresholds ride the same three verbs (I13C) but
resolve against I13's durable policy row, so their responses additionally carry
the generation, the optional UTC expiry, the pending reversion and the over-cap
rendering. Every one of those facts is computed in ``kew.service.config``: this
module derives no policy truth of its own, and neither may the web client.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from kew.config import (
    OVERRIDABLE_FIELDS,
    DispatchConfig,
    load_config,
    resolved_config_path,
)
from kew.serve.security import read_guard, require_write
from kew.service.config import (
    HUMAN_REVIEW_FIELDS,
    HUMAN_REVIEW_SECTION,
    OverrideDenied,
    OverrideNotFound,
    ReviewPolicyError,
    StaleGeneration,
    config_snapshot,
    human_review_state,
    resolve_config_field,
    revert_config_override,
    revert_human_review_override,
    set_config_override,
    set_human_review_override,
    valid_db_overrides,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

config_router = APIRouter(prefix="/api/config", tags=["config"])

_BUDGET_FIELDS = sorted(f for f in OVERRIDABLE_FIELDS if f.startswith("budget."))
_OVERRIDABLE_FIELDS = sorted(OVERRIDABLE_FIELDS)


def _file_config() -> DispatchConfig:
    """The config file's own layer — no DB override, and no environment.

    ``load_config()`` applies the ``KEW_*`` layer, so ``file_value`` used to be
    whatever the environment said (#2298). With ``KEW_MAX_BUDGET`` exported, the
    console showed the env number as the file's value AND the DB override beside
    it as if that override were in force, when env outranks it: the one surface
    whose job is to say which layer wins described a web override that changed
    nothing. ``env={}`` composes defaults → file → (no DB) and stops.
    """
    return load_config(env={})


def _field_resolution(
    field: str,
    *,
    file_cfg: DispatchConfig,
    effective_cfg: DispatchConfig,
    default_cfg: DispatchConfig,
    overrides: dict[str, Any],
) -> dict[str, Any]:
    """Full resolution story for one dotted field (the ``get_budget`` shape,
    plus ``default_value`` from a fresh defaults-only config layer)."""
    ov = overrides.get(field)
    return {
        "effective": resolve_config_field(effective_cfg, field),
        "file_value": resolve_config_field(file_cfg, field),
        "default_value": resolve_config_field(default_cfg, field),
        "override": None
        if ov is None
        else {
            "value": json.loads(ov["value_json"]),
            "set_by": ov["set_by"],
            "set_at": ov["set_at"],
            "reason": ov["reason"],
        },
    }


class OverrideBody(BaseModel):
    """One override edit. ``expires_at`` / ``if_generation`` are policy-only.

    ``if_generation`` is a compare-and-swap: state the generation the surface
    was showing and a concurrent edit is refused (409) instead of silently
    overwritten — which is also what stops a stale delete from removing a
    newer override.
    """

    value: Any
    reason: str = ""
    expires_at: str | None = None
    if_generation: int | None = None


@contextmanager
def _translated() -> Iterator[None]:
    """Map the service's refusals onto their HTTP statuses, in one place."""
    try:
        yield
    except (ValueError, ReviewPolicyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except OverrideDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from None
    except OverrideNotFound:
        raise HTTPException(status_code=404, detail="no override to revert") from None
    except StaleGeneration as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


def _is_review_field(field: str) -> bool:
    return field in HUMAN_REVIEW_FIELDS or field == HUMAN_REVIEW_SECTION


def _review_response(field: str, state: Any) -> dict[str, Any]:
    """The policy state, plus the ``field``/``effective`` pair every write returns."""
    rendered = state.as_dict()
    effective = state.values[field] if field in HUMAN_REVIEW_FIELDS else state.values
    return {"field": field, "effective": effective, **rendered}


def _refuse_policy_only_fields(body: OverrideBody) -> None:
    """An expiry or a generation on an ordinary override is a caller error.

    Accepting them silently would read as "this budget override expires
    tonight" when nothing would ever expire it.
    """
    if body.expires_at is not None or body.if_generation is not None:
        raise HTTPException(
            status_code=400,
            detail="expires_at and if_generation apply only to human_review fields",
        )


@config_router.get("/budget", dependencies=[Depends(read_guard)])
def get_budget(request: Request) -> dict[str, Any]:
    file_cfg = _file_config()
    with request.app.state.store_factory() as store:
        effective_cfg = load_config(store=store)
        overrides = {r["field"]: r for r in store.list_config_overrides()}

    result: dict[str, Any] = {}
    for field in _BUDGET_FIELDS:
        file_val = resolve_config_field(file_cfg, field)
        effective = resolve_config_field(effective_cfg, field)
        ov = overrides.get(field)
        if ov is not None:
            result[field] = {
                "effective": effective,
                "file_value": file_val,
                "override": {
                    "value": json.loads(ov["value_json"]),
                    "set_by": ov["set_by"],
                    "set_at": ov["set_at"],
                    "reason": ov["reason"],
                },
            }
        else:
            result[field] = {
                "effective": effective,
                "file_value": file_val,
                "override": None,
            }
    return result


@config_router.get("/overridable", dependencies=[Depends(read_guard)])
def get_overridable(request: Request) -> dict[str, dict[str, Any]]:
    """Every ``OVERRIDABLE_FIELDS`` field with its full resolution story,
    grouped by config section (``budget`` / ``agent`` / ``routing`` /
    ``breaker``). Each field carries ``effective`` / ``file_value`` /
    ``default_value`` / ``override`` — the generalized ``get_budget`` shape.

    A thin read shim: ``default_value`` comes from a fresh ``DispatchConfig()``
    (no file, no override, no env) — the pure defaults layer that ``load_config``
    composes on top of.
    """
    file_cfg = _file_config()
    default_cfg = DispatchConfig()
    with request.app.state.store_factory() as store:
        effective_cfg = load_config(store=store)
        overrides = {r["field"]: r for r in store.list_config_overrides()}

    grouped: dict[str, dict[str, Any]] = {}
    for field in _OVERRIDABLE_FIELDS:
        section = field.split(".", 1)[0]
        grouped.setdefault(section, {})[field] = _field_resolution(
            field,
            file_cfg=file_cfg,
            effective_cfg=effective_cfg,
            default_cfg=default_cfg,
            overrides=overrides,
        )
    return grouped


@config_router.get("/effective", dependencies=[Depends(read_guard)])
def get_effective(request: Request) -> dict[str, Any]:
    """Every tracked field, its effective value, and the layer that won it.

    The same computation ``kew config effective --sources`` prints
    (``kew.service.config.config_snapshot``), so the console and the CLI cannot
    disagree about which of the four layers — ``env (NAME)``, ``DB override``,
    ``kew.toml``, ``default`` — is in force. Read-only, bounded by the tracked
    field list, and it makes no GitHub call.

    It carries the ceilings the override page omits (``CAP_FIELDS``) because
    those cannot be edited from the DB: a cap nobody can see is the one that
    surprises an operator mid-epic.
    """
    config_path = resolved_config_path()
    with request.app.state.store_factory() as store:
        effective_cfg = load_config(store=store)
        overrides = valid_db_overrides(store)
        review, review_unavailable = _review_state(store, effective_cfg)
    fields = config_snapshot(
        effective_cfg,
        config_path=config_path,
        overrides=overrides,
        review=review,
    )
    return {
        "config_path": None if config_path is None else str(config_path),
        "fields": fields,
        # "" when the policy was read. Non-empty names why it could not be, so
        # the `human_review.*` rows showing config-layer values are explainable.
        "human_review_unavailable": review_unavailable,
    }


def _review_state(store: Any, config: DispatchConfig) -> tuple[Any, str]:
    """``(policy_state, why_absent)`` — the second half is the point.

    A project that names no canonical delivery repository has no human-review
    policy, and the three thresholds then come from the config layer like any
    other field. That is a legitimate state, but it must not read as "the policy
    happens to be empty": the refusal's own sentence travels in the response, so
    a reader of a thin-looking payload can tell which of the two it is.

    Read-only: an expired override reverts durably on the next *write*, and this
    read reports the reverted values without persisting them — the same stance
    ``kew config effective`` takes.
    """
    try:
        return human_review_state(store, config=config), ""
    except ReviewPolicyError as exc:
        return None, str(exc)


@config_router.get("/human_review", dependencies=[Depends(read_guard)])
def get_human_review(request: Request) -> dict[str, Any]:
    """The human-review policy in force, exactly as the service renders it."""
    with _translated(), request.app.state.store_factory() as store:
        return human_review_state(store, config=request.app.state.config).as_dict()


def _set_review_override(
    field: str, body: OverrideBody, request: Request
) -> dict[str, Any]:
    with _translated(), request.app.state.store_factory() as store:
        state = set_human_review_override(
            store,
            field=field,
            value=body.value,
            reason=body.reason,
            expires_at=body.expires_at,
            if_generation=body.if_generation,
            config=request.app.state.config,
            source="web",
        )
    return _review_response(field, state)


@config_router.post("/{field}")
def set_override(
    field: str,
    body: OverrideBody,
    request: Request,
    _: object = Depends(require_write),
) -> dict[str, Any]:
    """Thin shim: the validate → gate → audit → write flow lives in kew.service."""
    if field in HUMAN_REVIEW_FIELDS:
        return _set_review_override(field, body, request)
    _refuse_policy_only_fields(body)
    with _translated(), request.app.state.store_factory() as store:
        result = set_config_override(
            store,
            field=field,
            value=body.value,
            reason=body.reason,
            config=request.app.state.config,
            source="web",
        )
    return {"field": result.field, "effective": result.effective}


def _revert_review_override(field: str, request: Request) -> dict[str, Any]:
    with _translated(), request.app.state.store_factory() as store:
        state = revert_human_review_override(
            store,
            field=field,
            config=request.app.state.config,
            source="web",
        )
    return _review_response(field, state)


@config_router.delete("/{field}")
def revert_override(
    field: str,
    request: Request,
    _: object = Depends(require_write),
) -> dict[str, Any]:
    """Thin shim: the validate → gate → audit → clear flow lives in kew.service."""
    if _is_review_field(field):
        return _revert_review_override(field, request)
    with _translated(), request.app.state.store_factory() as store:
        result = revert_config_override(
            store,
            field=field,
            config=request.app.state.config,
            source="web",
        )
    return {"field": result.field, "effective": result.effective}
