"""Service shim for config overrides: validate → gate → audit → write.

The serve route reaches config-override writes only through here (kew.serve may
not import kew.audit / kew.run_store / kew.approval). Spend-authorizing overrides
pass the ``[approval].approvers`` allowlist gate and bind governance policy —
exactly as ``apply_approval_decision`` does — and the chained audit event is
emitted BEFORE the store write (Core security stance: attest, then act).

The same flow edits the three numeric ``[human_review]`` thresholds (I13C),
except that those land in I13's durable policy row rather than in
``config_overrides`` — see :data:`HUMAN_REVIEW_FIELDS`. Three authorities stay
distinct here: **spend approval** and **policy-change authority** are the
``[approval].approvers`` gate below, while **terminal judgment** (releasing one
publication hold) is ``[human_review].approvers``, enforced in
``kew.human_review.authorize_approver`` and never from this surface.
"""

from __future__ import annotations

import json
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ValidationError

from kew.approval import _authorized
from kew.audit import (
    bind_policy_context,
    current_policy_hash,
    record_current_policy,
    resolve_actor,
)
from kew.config import (
    _ENV_OVERRIDES,
    OVERRIDABLE_FIELDS,
    HumanReviewConfig,
    env_source,
    load_config,
    validate_override,
)

# ``ReviewPolicyError`` is imported (and re-exported below) so the serve edge
# can map "this config names no canonical delivery repository" onto a 400
# without reaching past this shim into the domain module.
from kew.human_review import (
    REVERSION_EXPIRED,
    ReviewPolicy,
    ReviewPolicyError,
    policy_from_config,
    resolve_policy,
)

__all__ = [
    "CAP_FIELDS",
    "HUMAN_REVIEW_FIELDS",
    "HUMAN_REVIEW_SECTION",
    "OverrideDenied",
    "OverrideNotFound",
    "OverrideResult",
    "ReviewPolicyError",
    "ReviewPolicyState",
    "StaleGeneration",
    "config_snapshot",
    "env_source",
    "file_layer_values",
    "human_review_state",
    "is_spend_authorizing",
    "resolve_config_field",
    "revert_config_override",
    "revert_human_review_override",
    "set_config_override",
    "set_human_review_override",
    "tracked_config_fields",
    "valid_db_overrides",
]

if TYPE_CHECKING:
    from kew.config import DispatchConfig
    from kew.run_store import RunStore, SqliteRunStore


class OverrideDenied(Exception):
    """Actor is not in ``[approval].approvers`` for a spend-authorizing field."""


class OverrideNotFound(Exception):
    """Revert requested for a field with no active override."""


@dataclass(frozen=True)
class OverrideResult:
    """The authoritative post-write state for the calling surface."""

    field: str
    effective: Any


#: Fields explicitly classified as NON-spend-authorizing (settable without the
#: approver gate). Empty today: every OVERRIDABLE_FIELD moves money or the model,
#: so unlisted fields default to the strong gate (spend-authorizing).
_NON_SPEND_FIELDS: frozenset[str] = frozenset()


def is_spend_authorizing(field: str) -> bool:
    """Default-strong: a field is spend-authorizing unless explicitly classified.

    Every currently-overridable field (``budget.*``, ``agent.max_budget_usd``,
    ``agent.model``, ``routing.*``) authorizes spend or steers the model, so a
    future field is gated unless it is deliberately added to
    ``_NON_SPEND_FIELDS``.
    """
    return field not in _NON_SPEND_FIELDS


def resolve_config_field(cfg: Any, dotted: str) -> Any:
    """Read a dotted field (e.g. ``budget.per_issue_usd``) as a JSON-safe value.

    A nested model (RouteEntry, section models) is dumped to a plain dict, and an
    absent section (no ``[routing]`` in the file) reads as ``None`` rather than
    raising — both needed by the routing/model overrides (#659).
    """
    section, attr = dotted.split(".", 1)
    section_obj = getattr(cfg, section, None)
    if section_obj is None:
        return None
    val = getattr(section_obj, attr, None)
    if isinstance(val, BaseModel):
        return val.model_dump()
    return val


def _gate_and_bind(
    store: RunStore, field: str, config: DispatchConfig, actor_id: str
) -> str | None:
    """Approver gate + policy binding for spend-authorizing fields.

    Raises :class:`OverrideDenied` when *actor_id* is not permitted. Mirrors
    ``apply_approval_decision``'s ``bind_policy_context`` / ``record_current_policy``
    sequencing (no behavior change). Returns the bound ``policy_hash`` (or None
    for a non-spend field).
    """
    if not is_spend_authorizing(field):
        return None
    if not _authorized(config, actor_id):
        raise OverrideDenied(f"actor {actor_id!r} is not in [approval].approvers")
    bind_policy_context(config)
    record_current_policy(store)
    return current_policy_hash()


def _provenance(
    *,
    field: str,
    old_value: Any,
    new_value: Any,
    actor_id: str,
    source: str,
    reason: str,
) -> dict[str, Any]:
    """Structured provenance always carried by an override audit event.

    ``reason`` is an optional human annotation (may be empty); the rest is
    auto-composed: field, old→new, actor, and the source surface.
    """
    return {
        "field": field,
        "file_value": old_value,
        "new_value": new_value,
        "actor": actor_id,
        "source": source,
        "reason": reason,
    }


#: Model fields whose override must name a catalog row (#2741). The reviewer's
#: model decides who judges agent PRs, so it is held to the catalog on write:
#: an id the catalog does not list, a retired or unroutable row, or a model on
#: a runner the reviewer does not run is refused before anything is attested.
_CATALOG_MODEL_FIELDS: frozenset[str] = frozenset({"review.model"})


def _require_catalog_model(field: str, value: str, config: DispatchConfig) -> None:
    """Raise ``ValueError`` unless *value* is a usable catalog id for *field*.

    Only the write is gated. A stored override whose row is later retired keeps
    loading (``validate_override`` does not read the catalog), so a catalog
    refresh can never silently move the reviewer onto another model.
    """
    from kew.model_catalog import load_catalog
    from kew.service.health import review_lane_runner

    row = load_catalog().get(value)
    if row is None:
        raise ValueError(f"{field}: {value!r} is not a model id in the catalog")
    if row.lifecycle.status == "retired":
        raise ValueError(f"{field}: {value!r} is retired")
    if not row.routable:
        raise ValueError(f"{field}: {value!r} is not routable")
    runner = review_lane_runner(config)
    if row.runner != runner:
        raise ValueError(
            f"{field}: {value!r} runs on {row.runner}, but the reviewer runs on "
            f"{runner}"
        )


def set_config_override(
    store: RunStore,
    *,
    field: str,
    value: Any,
    reason: str = "",
    config: DispatchConfig,
    source: str = "web",
) -> OverrideResult:
    """Validate → gate → audit → write a single config override.

    Raises ``ValueError`` for an off-allowlist field or invalid value (the edge
    maps this to 400) and :class:`OverrideDenied` for a blocked actor (403). The
    audit event is appended before the store write.
    """
    validate_override(field, value)  # ValueError -> 400 at the edge
    if field in _CATALOG_MODEL_FIELDS:
        _require_catalog_model(field, value, config)

    actor = resolve_actor()
    policy_hash = _gate_and_bind(store, field, config, actor.id)

    file_val = resolve_config_field(load_config(), field)
    now = datetime.now(UTC).isoformat()

    audit_event_id = store.append_audit_event(
        timestamp=now,
        event="config_override_set",
        actor_type=actor.type,
        actor_id=actor.id,
        repo=config.project.repo or None,
        policy_hash=policy_hash,
        payload=_provenance(
            field=field,
            old_value=file_val,
            new_value=value,
            actor_id=actor.id,
            source=source,
            reason=reason,
        ),
    )
    store.set_config_override(
        field=field,
        value_json=json.dumps(value),
        file_value_json=json.dumps(file_val),
        set_at=now,
        set_by=source,
        reason=reason,
        audit_event_id=audit_event_id,
    )
    return OverrideResult(field=field, effective=value)


def revert_config_override(
    store: RunStore,
    *,
    field: str,
    config: DispatchConfig,
    source: str = "web",
) -> OverrideResult:
    """Validate → gate → audit → clear an override, reverting to the file value.

    Raises ``ValueError`` for an off-allowlist field (400),
    :class:`OverrideNotFound` when there is nothing to revert (404), and
    :class:`OverrideDenied` for a blocked actor (403). The audit event is
    appended before the store clears the row.
    """
    if field not in OVERRIDABLE_FIELDS:
        raise ValueError(f"field {field!r} is not overridable")
    if store.get_config_override(field) is None:
        raise OverrideNotFound(field)

    actor = resolve_actor()
    policy_hash = _gate_and_bind(store, field, config, actor.id)

    file_val = resolve_config_field(load_config(), field)
    now = datetime.now(UTC).isoformat()

    store.append_audit_event(
        timestamp=now,
        event="config_override_reverted",
        actor_type=actor.type,
        actor_id=actor.id,
        repo=config.project.repo or None,
        policy_hash=policy_hash,
        payload={
            "field": field,
            "reverted_to_file_value": file_val,
            "actor": actor.id,
            "source": source,
        },
    )
    store.revert_config_override(field)
    return OverrideResult(field=field, effective=file_val)


# ── the human-review policy (I13C) ──────────────────────────────────
#
# The three numeric human_review fields are edited through this same
# validate → gate → audit → write flow, but they do NOT land in
# ``config_overrides``. They land in I13's durable policy row, which is what
# carries the monotonic generation, the optional UTC expiry, and the audited
# system reversion an expiry produces. Aliasing the two stores would give the
# policy an edit with no generation and no reversion evidence, and would let a
# ``load_config`` layer disagree with the policy the publication path enforces.

HUMAN_REVIEW_SECTION = "human_review"
_HR_ATTRS = ("max_pending_reviews", "max_diff_lines", "max_paths")

#: The three numbers this surface may edit. ``approvers`` is deliberately not
#: here: who may give terminal judgement is a different authority from what
#: the thresholds are, and it is not editable from a config surface.
HUMAN_REVIEW_FIELDS: frozenset[str] = frozenset(
    f"{HUMAN_REVIEW_SECTION}.{attr}" for attr in _HR_ATTRS
)

_HR_SET_EVENT = "human_review_override_set"
_HR_REVERT_EVENT = "human_review_override_reverted"


class StaleGeneration(Exception):
    """The caller edited a generation that is no longer the one in force.

    Two surfaces editing the same policy is the ordinary case (web, CLI, an
    operator's script), so an edit may state the generation it saw. A stale
    one is refused rather than applied on top: it would silently discard the
    newer edit, and — for a delete — a stale expiry would remove a generation
    its author never saw.
    """


@dataclass(frozen=True)
class ReviewPolicyState:
    """The human-review policy in force, as every surface must render it.

    ``over_cap`` is a *rendering*, never an eviction. Lowering capacity below
    occupancy leaves every hold exactly where it is; what it changes is that
    no new one may be taken until occupancy falls back under the limit.
    """

    delivery_repo: str
    values: dict[str, int]
    generation: int
    expires_at: str | None
    source: str
    approvers: tuple[str, ...]
    pending_reviews: int
    over_cap: bool
    pending_reversion: dict[str, Any] | None

    def as_dict(self) -> dict[str, Any]:
        """The JSON shape shared by the API, the CLI and the web client."""
        return {
            "delivery_repo": self.delivery_repo,
            "values": dict(self.values),
            "generation": self.generation,
            "expires_at": self.expires_at,
            "source": self.source,
            "approvers": list(self.approvers),
            "pending_reviews": self.pending_reviews,
            "over_cap": self.over_cap,
            "pending_reversion": self.pending_reversion,
        }


def _hr_attr(field: str) -> str:
    if field not in HUMAN_REVIEW_FIELDS:
        raise ValueError(f"field {field!r} is not a human_review policy field")
    return field.split(".", 1)[1]


def _hr_value(field: str, value: Any) -> int:
    """Round-trip one value through the owning model, as overrides already do."""
    attr = _hr_attr(field)
    try:
        parsed = HumanReviewConfig.model_validate({attr: value})
    except ValidationError as exc:
        raise ValueError(f"{field}: {exc.errors()[0]['msg']}") from None
    return int(getattr(parsed, attr))


def _hr_expiry(expires_at: str | None, now: datetime) -> str | None:
    """Normalize an optional expiry to explicit UTC, refusing a dead one."""
    if expires_at is None:
        return None
    try:
        parsed = datetime.fromisoformat(expires_at)
    except (TypeError, ValueError):
        raise ValueError(
            f"expires_at {expires_at!r} is not an ISO-8601 timestamp"
        ) from None
    if parsed.tzinfo is None:
        raise ValueError("expires_at must carry an explicit UTC offset")
    utc = parsed.astimezone(UTC)
    if utc <= now:
        raise ValueError("expires_at is already past — that override never applies")
    return utc.isoformat()


def _hr_values(policy: ReviewPolicy) -> dict[str, int]:
    return {f"{HUMAN_REVIEW_SECTION}.{a}": int(getattr(policy, a)) for a in _HR_ATTRS}


def _hr_reversion(
    system: ReviewPolicy, override: ReviewPolicy
) -> dict[str, Any] | None:
    """What the policy will revert to, and when, if an expiry is pending."""
    if override.expires_at is None:
        return None
    return {
        "at": override.expires_at,
        "reason": REVERSION_EXPIRED,
        "to": {**_hr_values(system), "generation": override.generation + 1},
    }


def _replaced(
    policy: ReviewPolicy, changes: dict[str, Any], **fixed: Any
) -> ReviewPolicy:
    """``dataclasses.replace`` with the edited thresholds named at runtime."""
    return replace(policy, **changes, **fixed)


def _check_generation(expected: int | None, current: int) -> None:
    if expected is not None and expected != current:
        raise StaleGeneration(
            f"policy generation {expected} is stale; {current} is in force"
        )


def human_review_state(
    store: SqliteRunStore,
    *,
    config: DispatchConfig,
    now: datetime | None = None,
) -> ReviewPolicyState:
    """Render the policy in force for this delivery repository. Never writes.

    An expired override reads as the system policy under the generation the
    reversion will carry, but rendering it does not *perform* the reversion:
    a read is not an effect, and the durable, audited reversion belongs to the
    writers (an edit here, or the publication path in I14).
    """
    moment = now or datetime.now(UTC)
    system = policy_from_config(config)
    override = store.get_human_review_policy(system.delivery_repo)
    resolution = resolve_policy(system=system, override=override, now=moment)
    live = None if resolution.reverted else override
    policy = resolution.policy
    pending = store.pending_review_count(system.delivery_repo)
    return ReviewPolicyState(
        delivery_repo=policy.delivery_repo,
        values=_hr_values(policy),
        generation=policy.generation,
        expires_at=policy.expires_at,
        source="system" if live is None else "override",
        approvers=policy.approvers,
        pending_reviews=pending,
        over_cap=pending > policy.max_pending_reviews,
        pending_reversion=None if live is None else _hr_reversion(system, live),
    )


def _current_policy(
    store: SqliteRunStore, config: DispatchConfig, now: datetime
) -> ReviewPolicy:
    """The policy an edit builds on, durably reverting an expired one first.

    This is the write path's first act on purpose: an edit computed on top of
    an override the clock already killed would resurrect it under a new
    generation, and the reversion would never be attested.
    """
    return store.effective_human_review_policy(
        system=policy_from_config(config), now=now
    )


def _hr_audit(
    store: SqliteRunStore,
    *,
    event: str,
    payload: dict[str, Any],
    config: DispatchConfig,
    repo: str,
    actor: Any,
    policy_hash: str | None,
    now: datetime,
) -> int | None:
    """Attest, then act — the same ordering the generic override path uses."""
    return store.append_audit_event(
        timestamp=now.isoformat(),
        event=event,
        actor_type=actor.type,
        actor_id=actor.id,
        repo=repo,
        policy_hash=policy_hash,
        payload=payload,
    )


def set_human_review_override(
    store: SqliteRunStore,
    *,
    field: str,
    value: Any,
    config: DispatchConfig,
    reason: str = "",
    expires_at: str | None = None,
    if_generation: int | None = None,
    source: str = "web",
    now: datetime | None = None,
) -> ReviewPolicyState:
    """Validate → gate → audit → write one human_review threshold.

    Raises ``ValueError`` for an off-policy field, an unusable value or a
    naive/past expiry (400 at the edge), :class:`OverrideDenied` for a blocked
    actor (403), and :class:`StaleGeneration` when the caller edited a
    generation that is no longer current (409).
    """
    moment = now or datetime.now(UTC)
    attr = _hr_attr(field)
    parsed = _hr_value(field, value)
    expiry = _hr_expiry(expires_at, moment)

    actor = resolve_actor()
    policy_hash = _gate_and_bind(store, field, config, actor.id)
    current = _current_policy(store, config, moment)
    _check_generation(if_generation, current.generation)
    proposed = _replaced(
        current,
        {attr: parsed},
        generation=current.generation + 1,
        expires_at=expiry,
    )

    event_id = _hr_audit(
        store,
        event=_HR_SET_EVENT,
        payload={
            "field": field,
            "old_value": getattr(current, attr),
            "new_value": parsed,
            "actor": actor.id,
            "source": source,
            "reason": reason,
            "generation": proposed.generation,
            "previous_generation": current.generation,
            "expires_at": expiry,
        },
        config=config,
        repo=current.delivery_repo,
        actor=actor,
        policy_hash=policy_hash,
        now=moment,
    )
    store.set_human_review_policy(
        policy=proposed,
        actor=actor.id,
        reason=reason,
        audit_event_id=event_id,
        now=moment,
    )
    return human_review_state(store, config=config, now=moment)


def _revert_attrs(field: str) -> tuple[str, ...]:
    """Which thresholds a revert restores — one field, or the whole policy."""
    if field == HUMAN_REVIEW_SECTION:
        return _HR_ATTRS
    return (_hr_attr(field),)


def revert_human_review_override(
    store: SqliteRunStore,
    *,
    config: DispatchConfig,
    field: str = HUMAN_REVIEW_SECTION,
    reason: str = "",
    if_generation: int | None = None,
    source: str = "web",
    now: datetime | None = None,
) -> ReviewPolicyState:
    """Restore one threshold — or the whole policy — to its system value.

    Deleting the last overridden threshold is expressed through I13's expiry
    primitive rather than a bare row delete, so the store appends the chained
    reversion event *before* the row goes away. Raises
    :class:`OverrideNotFound` when nothing is overridden (404).
    """
    moment = now or datetime.now(UTC)
    attrs = _revert_attrs(field)
    actor = resolve_actor()
    policy_hash = _gate_and_bind(store, field, config, actor.id)

    system = policy_from_config(config)
    current = _current_policy(store, config, moment)
    if store.get_human_review_policy(system.delivery_repo) is None:
        raise OverrideNotFound(field)
    _check_generation(if_generation, current.generation)

    restored = _replaced(
        current,
        {attr: getattr(system, attr) for attr in attrs},
        generation=current.generation + 1,
    )
    event_id = _hr_audit(
        store,
        event=_HR_REVERT_EVENT,
        payload={
            "field": field,
            "reverted_to": {a: getattr(system, a) for a in attrs},
            "actor": actor.id,
            "source": source,
            "reason": reason,
            "generation": restored.generation,
            "previous_generation": current.generation,
        },
        config=config,
        repo=system.delivery_repo,
        actor=actor,
        policy_hash=policy_hash,
        now=moment,
    )
    _write_revert(
        store,
        restored=restored,
        system=system,
        actor=actor.id,
        reason=reason,
        event_id=event_id,
        now=moment,
    )
    return human_review_state(store, config=config, now=moment)


def _write_revert(
    store: SqliteRunStore,
    *,
    restored: ReviewPolicy,
    system: ReviewPolicy,
    actor: str,
    reason: str,
    event_id: int | None,
    now: datetime,
) -> None:
    """Write the narrowed override, or expire it away when nothing is left."""
    fully_reverted = _hr_values(restored) == _hr_values(system)
    store.set_human_review_policy(
        policy=replace(restored, expires_at=now.isoformat())
        if fully_reverted
        else restored,
        actor=actor,
        reason=reason,
        audit_event_id=event_id,
        now=now,
    )
    if fully_reverted:
        # The store attests the reversion and then drops the row, in one
        # transaction — the same path a clock-driven expiry takes.
        store.effective_human_review_policy(system=system, now=now)


# ── 4-layer provenance: the snapshot both `kew config effective` and
#    `GET /api/config/effective` serve (#2298) ───────────────────────────────
#
# Lifted out of `kew.cli.config` so the CLI and the web console answer "what is
# in force, and which layer put it there?" from ONE computation. Two surfaces
# resolving provenance separately is how `/api/config/budget` came to call an
# env-sourced number the config file's own value.

#: Caps that govern how much work and money a dispatch may consume, which the
#: console's override page does not list because none of them is overridable
#: from the DB. A health reader needs them anyway: "is the factory able to
#: work?" is partly a question about its ceilings, and a ceiling nobody can see
#: is the one that surprises an operator at 2am.
CAP_FIELDS: frozenset[str] = frozenset(
    {
        "review.max_rounds",
        "review.max_review_usd",
        "review.max_remediation_usd",
        "review.stop_rule_after_rounds",
        "parallel.max_workers",
        "epic.max_parallel",
        "retry.max_attempts",
        "agent.timeout_minutes",
    }
)

#: Sandbox fields whose effective value is a posture claim, so they belong in
#: any provenance read even though neither is DB-overridable.
_SANDBOX_FIELDS: frozenset[str] = frozenset(
    {"sandbox.enabled", "sandbox.allow_uncontained_flows"}
)


def tracked_config_fields() -> tuple[str, ...]:
    """Fields whose effective value can differ from the packaged default.

    The DB layer is allowlisted (``OVERRIDABLE_FIELDS``), the environment layer
    contributes its operational selectors (repo, log directory, worker count),
    and the rest are the policy and capacity ceilings a reader has to be able
    to see — :data:`CAP_FIELDS` and the two sandbox posture switches.
    """
    env_fields = {f"{section}.{field}" for section, field, _ in _ENV_OVERRIDES.values()}
    return tuple(
        sorted(
            OVERRIDABLE_FIELDS
            | env_fields
            | HUMAN_REVIEW_FIELDS
            | _SANDBOX_FIELDS
            | CAP_FIELDS
        )
    )


def file_layer_values(path: Path | None, fields: tuple[str, ...]) -> dict[str, Any]:
    """Read only the values *path* states explicitly — no DB layer, no env layer.

    This is the ``kew.toml`` layer of the provenance, and it is read off the
    TOML rather than off a loaded config precisely so no other layer can be
    mistaken for it.
    """
    if path is None or not path.is_file():
        return {}
    with path.open("rb") as handle:
        raw = tomllib.load(handle)
    values: dict[str, Any] = {}
    for field in fields:
        section, attr = field.split(".", 1)
        section_raw = raw.get(section)
        if isinstance(section_raw, dict) and attr in section_raw:
            values[field] = section_raw[attr]
    return values


def valid_db_overrides(store: Any) -> dict[str, Any]:
    """Decode the usable DB overrides; an invalid legacy row wins no provenance.

    A row that no longer validates is TOLERATED and skipped, never rewritten —
    the ledger is read-only to every reader.
    """
    result: dict[str, Any] = {}
    for row in store.list_config_overrides():
        field = row["field"]
        if field not in OVERRIDABLE_FIELDS:
            continue
        try:
            value = json.loads(row["value_json"])
            validate_override(field, value)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        result[field] = value
    return result


def _policy_source(review: ReviewPolicyState) -> str:
    """Name the edit in force, not just the layer — a policy has a generation."""
    detail = f"generation {review.generation}"
    if review.expires_at is not None:
        detail += f", expires {review.expires_at}"
    return f"human_review policy ({detail})"


def _review_entry(field: str, review: ReviewPolicyState) -> dict[str, Any]:
    """One human_review threshold as the durable policy states it.

    The value comes from the policy rather than from the config layer: an
    override that ``kew.toml`` cannot see is exactly the case this exists to
    make visible. ``over_cap`` rides along because a capacity that is under its
    own occupancy is the one policy fact a bare number hides.
    """
    return {
        "value": review.values[field],
        "source": _policy_source(review)
        if review.source == "override"
        else "human_review policy (system values)",
        "generation": review.generation,
        "expires_at": review.expires_at,
        "over_cap": review.over_cap,
    }


def config_snapshot(
    config: Any,
    *,
    config_path: Path | None,
    overrides: dict[str, Any],
    review: ReviewPolicyState | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Every tracked field's effective value and the layer that won it.

    The four layers, highest first: ``env (NAME)``, ``DB override``,
    ``kew.toml``, ``default``. The ``kew.toml`` layer is the file's own text
    (:func:`file_layer_values`), so an environment variable can never be
    reported as something the file said.
    """
    fields = tracked_config_fields()
    file_values = file_layer_values(config_path, fields)
    result: dict[str, dict[str, Any]] = {}
    for field in fields:
        if review is not None and field in HUMAN_REVIEW_FIELDS:
            result[field] = _review_entry(field, review)
            continue
        env_var = env_source(field, env)
        if env_var is not None:
            source = f"env ({env_var})"
        elif field in overrides:
            source = "DB override"
        elif field in file_values:
            source = "kew.toml"
        else:
            source = "default"
        result[field] = {
            "value": resolve_config_field(config, field),
            "source": source,
        }
    if config.sandbox.allow_uncontained_flows is True:
        entry = result["sandbox.allow_uncontained_flows"]
        entry["value"] = config.sandbox.uncontained_flows_allowed
        if not entry["value"]:
            entry["configured_value"] = True
            entry["source"] += " (no-op: requires sandbox.enabled = true)"
    return result
