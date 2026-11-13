"""The dispatch launchpad's read (#2302) — the resolved route, with provenance.

One question: *if this item were dispatched now, what exactly would run, and
which rule decided each part of that?* Every field comes back with its own
``source`` string, because the launchpad's whole job is to let an operator see
that ``codex`` is about to run because of ``kew.toml [routing].simple`` — and a
surface that computed the route itself would be a second resolver, free to
disagree with the one that launches.

So nothing here decides anything. The runner and model come from
``runners.registry.explain_runner_route`` (``resolve_runner``'s sibling, which
names the entry it walked), containment and the fingerprint from
``probe_route.resolve_probe_route`` (which spends nothing, and is the half of
the readiness slice a read-only surface is allowed to reach), the money from
``budget.effective_budget_view``, the swap advice from the cache governor, and
the host facts — is the image here, is the credential set — from the health
node's own ``ProbeCache``, warmed by the serve sweep.

Three rules this module is written to:

* **No tracker call.** The complexity comes from the caller; absent, it is the
  tier label the ready queue's last read found on the item (#2785) — a peek at
  the console's own cache, never a read — and absent there too it is
  ``unknown`` with ``known: false`` and the route for the default tier. kew's
  own classifier reads the item's LABELS, and reading those is a GitHub call
  this endpoint may not make.
* **It writes nothing.** No audit row, no reservation, no probe lease. A plan is
  a reading, and the same rule is what ``kew dispatch --dry-run`` was breaking.
* **No secret value.** Credentials travel as ``{name, set}``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from kew.budget import effective_budget_view
from kew.config import env_source
from kew.probe_route import resolve_probe_route
from kew.runners.registry import (
    ResolvedRoute,
    explain_runner_route,
    get_runner,
    runner_required_credentials,
)
from kew.sandbox import REFUSAL_OPENSHELL_UNCERTIFIED
from kew.service.dashboard import ready_queue_tier
from kew.service.health import HealthProbes, ProbeCache, review_lane_runner

if TYPE_CHECKING:
    from kew.config import DispatchConfig

_logger = logging.getLogger(__name__)

#: The lanes a plan can be resolved for. ``implement`` is the routed dispatch;
#: ``review`` and ``remediation`` both run as the reviewer (``[review] runner``)
#: and differ only in which pool their cap is drawn from.
LANES = ("implement", "review", "remediation")

#: The complexity tiers a caller may name. Anything else — including nothing at
#: all — is ``unknown``, which resolves the default tier and says so.
TIERS = ("simple", "medium", "complex")

#: How long a resolved route is reused. The resolution itself shells out to the
#: container runtime (an image tag must become an immutable ID before kew will
#: call a route contained), so a page that revalidates as the operator changes a
#: choice would otherwise probe docker per keystroke.
ROUTE_TTL_S = 60.0

_UNKNOWN = "unknown"

#: The live-status vocabulary, mirrored as the strings ``readiness_probe``
#: defines them (``LiveStatus``). Mirrored rather than imported, for the reason
#: ``service/work.py`` mirrors the probe states: that module is the one that can
#: SPEND, no read-only surface may be able to reach it, and a vocabulary is not
#: worth opening that door for.
_CONFIRMED = "confirmed"
_REFUTED = "refuted"
_UNVERIFIED = "unverified"

#: Everything ``resolve_probe_route`` raises when the route cannot be resolved
#: at all: no runtime (``SandboxRuntimeUnavailable``), no image on this host
#: (``ContainedImageUnresolvedError``), no transport capability
#: (``TransportResolutionError``), or an unknown runner (``ValueError``). Each
#: is a READING — "this lane cannot run here, and here is why" — so none of them
#: may reach a reader as a 500.
#:
#: The same tuple guards the step BEFORE that one (:func:`_lane_route`):
#: ``explain_runner_route`` raises the same ``ValueError`` for a config that
#: names a runner this build does not have, and a refusal is no less a reading
#: for having been reached one call earlier.
_ROUTE_REFUSALS = (RuntimeError, ValueError, KeyError, OSError)

#: Refusal causes that may not be held for ``ROUTE_TTL_S`` unconditionally.
#: The TTL's whole justification is that a refusal does not become false again
#: within the minute — true of "no docker on this host", false of the OpenShell
#: egress gate, which is a LEDGER reading: ``kew sandbox certify``, the remedy
#: the refusal itself prints, is an external actor that makes it false
#: immediately, and a cached posture would keep telling the operator the route
#: is unproven after they did exactly what they were told (reviewer finding on
#: #2319's merge). So each read re-checks the ledger MARK — one indexed row,
#: no host call — and re-derives only once a certification has landed since.
_LEDGER_REFUSALS = frozenset({REFUSAL_OPENSHELL_UNCERTIFIED})


#: Cap on a refusal reported as a reading, in characters.
#:
#: A refusal is written for an operator: one sentence naming what stopped the
#: lane, then the remedy — "Export ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN
#: (…) to dispatch sandboxed, or set [sandbox] dangerously_skip_containment =
#: true". The longest refusal kew itself writes is 461 characters and this cap
#: was 300, so the launchpad printed that remedy cut mid-word — "do not use
#: subscription au" — dropping the one instruction a refused lane exists to
#: hand over (#2445). The cap now clears every refusal kew writes whole, and
#: stays a cap because the text can also carry the tail of a child's stderr.
REFUSAL_MAX_CHARS = 600

#: How much of an over-long refusal's END survives the cut, behind an ellipsis.
#:
#: The diagnosis is written first and the remedy after it, so the tail is the
#: half an operator acts on — the same reading that made an over-long hold
#: reason's cut take the MIDDLE out rather than the end (#2436).
REFUSAL_TAIL_CHARS = 300

_REFUSAL_ELLIPSIS = "..."


def _refusal_line(exc: Exception) -> str:
    """A refusal as one printable line: short enough for a row, whole enough to act on.

    The row that shows it is one line, and the message is written for a
    terminal, so interior newlines are flattened. An over-long one keeps its
    head and its tail and loses the middle, and both cuts land on a space: a
    half-word either side of the ellipsis reads as a typo in the very command
    the operator is being told to type.

    Non-printables go with them, for the reason ``safe_hold_reason`` drops them
    from a hold reason: a refusal can carry the tail of a child's stderr, so
    this is untrusted text on its way to a terminal, a TUI and a browser.
    """
    flat = "".join(char for char in " ".join(str(exc).split()) if char.isprintable())
    if len(flat) <= REFUSAL_MAX_CHARS:
        return flat
    head = flat[: REFUSAL_MAX_CHARS - len(_REFUSAL_ELLIPSIS) - REFUSAL_TAIL_CHARS]
    head = head[: head.rfind(" ") + 1].rstrip() or head.rstrip()
    tail = flat[-REFUSAL_TAIL_CHARS:]
    tail = tail[tail.find(" ") + 1 :] or tail
    return f"{head}{_REFUSAL_ELLIPSIS}{tail}"


def _sourced(value: Any, source: str) -> dict[str, Any]:
    return {"value": value, "source": source}


@dataclass
class _Lane:
    """One lane's route, already resolved, and where each half came from.

    ``field`` / ``model_field`` are the dotted config fields the DB override
    table is keyed by, one per half, so an override on ``agent.model`` is
    reported against the model row and not against the runner's.

    ``route`` is ``None`` when the config names a runner this build does not
    have: there is no route to report and no provenance to report it against,
    so ``refusal`` carries the resolver's own message and the reading comes
    back ``posture: refused`` — the same shape as every other unresolvable
    route (:func:`_refused_lane`).
    """

    route: ResolvedRoute | None
    runner_source: str
    model_source: str
    field: str
    model_field: str = ""
    refusal: str = ""

    @property
    def runner(self) -> str:
        """The runner's name, or ``''`` when the route could not be resolved."""
        return "" if self.route is None else self.route.runner.name

    @property
    def model(self) -> str:
        return "" if self.route is None else self.route.model


def _review_model(config: DispatchConfig, runner_name: str) -> tuple[str, str, str]:
    """``(model, source, field)`` for the review lane, as the launcher resolves it.

    Mirrors ``graph/driver/review_launcher.review_model``, which the service
    layer may not import (``kew.service`` is the far side of the ``kew.graph``
    boundary), exactly as ``review_lane_runner`` mirrors ``review_runner``:
    ``[agent].model`` is the reviewer's model whenever ``[review].model`` is
    unset, and reading only the ``[review]`` pin reported '' for a run that
    really uses the agent model. '' is also what the route fingerprint is
    composed from, so the stored probe evidence for the real review boundary
    could never be matched (reviewer P1).
    ``test_the_review_lane_model_mirrors_the_launcher`` holds the two values to
    each other, so the mirror cannot drift back into a second answer.
    """
    if config.review.model:
        return config.review.model, "kew.toml [review].model", "review.model"
    if config.agent.model:
        return config.agent.model, "kew.toml [agent].model", "agent.model"
    # Neither pin is set, so the runner picks: a runner fact, not a config
    # value, and reported as the runner's default rather than invented here.
    return "", f"{runner_name}'s own default", ""


def _review_lane(config: DispatchConfig) -> _Lane:
    """The reviewer's own route: the ``[review]`` pins, not the node routing.

    ``review_lane_runner`` is the health node's mirror of the launcher's one
    resolution site, so this lane names the agent the review stage really
    starts, and ``_review_model`` mirrors the launcher's model fallback.
    """
    runner_name = review_lane_runner(config)
    model, model_source, model_field = _review_model(config, runner_name)
    return _Lane(
        route=ResolvedRoute(runner=get_runner(runner_name), model=model),
        runner_source=(
            "kew.toml [review].runner"
            if config.review.runner
            else "default (claude-code)"
        ),
        model_source=model_source,
        field="review.runner",
        model_field=model_field,
    )


def _refused_lane(exc: Exception) -> _Lane:
    """A lane whose RUNNER could not be resolved, as a reading.

    ``[agent] runner = "nope"`` loads — no config validator checks a runner
    name against the registry, because ``kew.config`` may not import
    ``kew.runners`` — and fails at the resolution, which is the same class of
    answer as "no docker on this host": this lane cannot run here, and here is
    why. So it comes back ``posture: refused`` with the resolver's message
    rather than as a 500 out of a read-only endpoint whose documented contract
    is that nothing here raises (reviewer finding on #2302).

    No field and no provenance: the resolver raised before it could name the
    entry it walked, and this module may not re-walk the routing table to guess
    one — a second resolver is the whole thing the endpoint exists to avoid.
    """
    return _Lane(
        route=None,
        runner_source="the configured runner could not be resolved",
        model_source="no runner resolved, so no model was",
        field="",
        refusal=_refusal_line(exc),
    )


def _lane_route(config: DispatchConfig, lane: str, complexity: str | None) -> _Lane:
    try:
        if lane == "implement":
            explained = explain_runner_route(config, complexity)
            return _Lane(
                route=explained.route,
                runner_source=explained.runner_source,
                model_source=explained.model_source,
                field=explained.field,
                model_field=explained.model_field,
            )
        return _review_lane(config)
    except _ROUTE_REFUSALS as exc:
        return _refused_lane(exc)


def _override_source(store: Any, config_field: str, source: str) -> str:
    """Name the layer above the file steering a field: the env, or a DB override.

    ``load_config`` has already applied both, so the value on screen is theirs
    — reporting the file as its source would send an operator to a ``kew.toml``
    line that is not what is running (the config page's wording, ``DB
    override``, is reused on purpose). Every row resolved from an overridable
    field goes through here for that reason: the runner, the model and both
    budget caps, because one row naming the override while its neighbour names
    a file line is the same wrong answer on half the screen (reviewer P2).

    The environment is asked first because ``load_config`` applies it after the
    DB layer, so ``KEW_RUNNER`` beats an ``agent.runner`` override just as it
    beats the file (``config_snapshot``'s four-layer order, #2302 review).

    An empty *config_field* names no field — the review lane's model when
    neither pin is set, which the runner decides — and is left as it came.
    """
    if not config_field:
        return source
    env_var = env_source(config_field)
    if env_var is not None:
        return f"env ({env_var})"
    try:
        override = store.get_config_override(config_field)
    except Exception as exc:  # noqa: BLE001 — a provenance read is never fatal
        _logger.warning("dispatch plan: override %r unreadable: %s", config_field, exc)
        return source
    if not override:
        return source
    by = override.get("set_by") or "?"
    return f"DB override ({config_field}, set by {by})"


# ── containment, transport, fingerprint ─────────────────────────────────────


@dataclass(frozen=True)
class _Resolved:
    """What one resolution attempt produced: a probe route, or a refusal.

    ``ledger_mark`` is set only for a refusal in ``_LEDGER_REFUSALS`` — one an
    external actor can overturn immediately — and carries the ledger's newest
    certification at the moment it was derived, so a later read can ask whether
    anything has been certified SINCE rather than re-deriving the refusal from
    the host. ``cacheable`` is false only when that mark could not be taken,
    because a refusal with no way to notice its remedy must not be held.
    """

    probe: Any | None = None
    refusal: str = ""
    cacheable: bool = True
    ledger_mark: int | None = None


def _ledger_mark(store: Any) -> int | None:
    """The ledger's newest egress certification, or ``None`` if unreadable.

    ``None`` is "I cannot tell whether the remedy has been run", which is the
    one state a ledger refusal may not be cached in.
    """
    from kew.audit import runtime_egress_certification_mark

    try:
        return runtime_egress_certification_mark(store)
    except Exception as exc:  # noqa: BLE001 — a cache guard is never fatal
        _logger.warning("dispatch plan: certification mark unreadable: %s", exc)
        return None


def _unchanged(store: Any, resolved: _Resolved) -> bool:
    """May this cached resolution still be served?

    Everything but a ledger refusal: yes, the TTL is the whole rule. A ledger
    refusal: only while no certification has been appended since it was
    derived — an unreadable mark re-derives, because "I could not check" is
    not "nothing changed".
    """
    if resolved.ledger_mark is None:
        return True
    return _ledger_mark(store) == resolved.ledger_mark


@dataclass
class RouteResolver:
    """``resolve_probe_route`` behind a TTL, with the health node's probe cache.

    ``kew.probe_route`` is the ONE site that resolves the leg a launch would
    cross, and it is importable here precisely because it cannot spend:
    ``kew.readiness_probe`` — the module that can take a lease and make a call —
    stays behind its single door (``kew doctor``), which
    ``tests/test_readiness_probe.py`` enforces.

    Held by the API across requests exactly as ``HealthReader`` is: the slow
    part is the container-runtime inspection behind a contained route, and the
    launchpad re-reads whenever a choice changes. A refusal is cached too —
    "no docker here" does not become true again within the minute, and
    re-deriving it costs the same probe that produced it.

    The ones in ``_LEDGER_REFUSALS`` are the exception, and they are held
    CONDITIONALLY rather than not at all: ``kew sandbox certify`` — the remedy
    the refusal itself prints — makes them false immediately, so each read
    re-checks the ledger mark (:func:`runtime_egress_certification_mark`, one
    indexed row) and re-derives only when a certification has landed since.
    Dropping the whole entry instead put the TWO subprocesses behind an
    OpenShell resolution — ``docker image inspect`` and ``openshell
    --version`` — on every read of an unrate-limited endpoint the launchpad
    re-reads per keystroke, which is the exact behaviour ``ROUTE_TTL_S``
    exists to prevent (#2302 review).
    """

    cache: ProbeCache = field(
        default_factory=lambda: ProbeCache(HealthProbes(), ttl_s=ROUTE_TTL_S)
    )
    ttl_s: float = ROUTE_TTL_S
    _routes: dict[tuple[str, str, str], tuple[float, _Resolved]] = field(
        default_factory=dict
    )

    def resolve(
        self, store: Any, config: DispatchConfig, lane: _Lane, complexity: str | None
    ) -> _Resolved:
        route = lane.route
        if route is None:
            # The lane's own refusal IS the answer, and there is no route to
            # probe for — so nothing is resolved and nothing is cached: the
            # next read costs one config resolution, which is free.
            return _Resolved(refusal=lane.refusal)
        key = (route.runner.name, route.model, complexity or "")
        now = datetime.now(UTC).timestamp()
        hit = self._routes.get(key)
        if hit is not None and now - hit[0] < self.ttl_s and _unchanged(store, hit[1]):
            return hit[1]
        resolved = self._resolve(store, config, route, complexity)
        if resolved.cacheable:
            self._routes[key] = (now, resolved)
        else:
            self._routes.pop(key, None)
        return resolved

    def _resolve(
        self,
        store: Any,
        config: DispatchConfig,
        route: ResolvedRoute,
        complexity: str | None,
    ) -> _Resolved:
        try:
            probe = resolve_probe_route(
                config, complexity=complexity, store=store, resolved=route
            )
        except _ROUTE_REFUSALS as exc:
            # The refusal IS the answer for this lane, so it is reported rather
            # than raised — and flattened to one bounded line, because it is a
            # message written for a terminal and the row that shows it is one
            # line (`_refusal_line` keeps the remedy at its end intact).
            ledger = getattr(exc, "reason", "") in _LEDGER_REFUSALS
            mark = _ledger_mark(store) if ledger else None
            return _Resolved(
                refusal=_refusal_line(exc),
                cacheable=not ledger or mark is not None,
                ledger_mark=mark,
            )
        return _Resolved(probe=probe)


def _containment(
    resolved: _Resolved, config: DispatchConfig, runner: str, cache: ProbeCache
) -> dict[str, Any]:
    """The posture this lane would launch under, plus the two host facts.

    ``image_present`` and each credential's ``set`` come from the probe cache
    the serve sweep warms (the health node's), never from a fresh probe per
    request. The credential list is names and a bit: the value is never read.
    """
    probe = resolved.probe
    runtime = config.sandbox.runtime
    image = None if probe is None else probe.image
    if probe is not None:
        runtime = probe.sandbox.runtime or runtime
    return {
        "posture": "refused" if probe is None else probe.sandbox.posture,
        "refusal_reason": resolved.refusal or None,
        "runtime": runtime,
        "image": image,
        "image_present": None if image is None else cache.image_present(runtime, image),
        "credential_names_present": [
            {"name": name, "set": cache.env_set(name)}
            for name in runner_required_credentials(runner)
        ],
        "source": "resolve_effective_sandbox, as dispatch resolves it",
    }


def _transport(resolved: _Resolved, config: DispatchConfig) -> dict[str, Any]:
    """The inference transport, as the launch would resolve it.

    The source is read off ``model_fields_set`` — the rule ``_cap_source``
    follows — not by comparing the configured mode against the default string:
    ``mode = "direct"`` written down on purpose and ``mode`` left unset resolve
    the same value, and a deliberate pin must not be reported as a default
    (reviewer P5).
    """
    probe = resolved.probe
    configured = config.inference_transport.mode
    source = (
        "kew.toml [inference_transport].mode"
        if "mode" in config.inference_transport.model_fields_set
        else f"default ({configured})"
    )
    if probe is None or probe.transport is None:
        return _sourced(configured, source)
    return _sourced(probe.transport.mode, source)


# ── money, governor, admission ──────────────────────────────────────────────


def _cap_source(config: DispatchConfig, store: Any, name: str) -> str:
    """Which layer set one budget cap: a DB override, ``kew.toml``, or neither.

    ``model_fields_set`` alone cannot answer this: ``_apply_db_overrides``
    merges an override into the raw dict BEFORE validation, so an overridden
    cap is indistinguishable from a hand-committed one in the parsed model —
    which is why the override is asked about first (reviewer P2, and the rule
    ``budget._runtime_cap_source`` already follows for the kill cap).
    """
    file_source = (
        f"kew.toml [budget].{name}"
        if name in config.budget.model_fields_set
        else "default"
    )
    return _override_source(store, f"budget.{name}", file_source)


def _budget(
    config: DispatchConfig, store: Any, issue: int | None, item_id: str | None
) -> dict[str, Any]:
    """The effective money picture, each cap carrying the layer that set it."""
    view = effective_budget_view(config, store, issue, item_id=item_id)
    return {
        "runtime_cap_usd": view.runtime_cap_usd,
        "runtime_cap_source": view.runtime_cap_source,
        "per_issue_cap_usd": view.per_issue_cap_usd,
        "per_issue_cap_source": _cap_source(config, store, "per_issue_usd"),
        "issue_spent_usd": view.issue_spent_usd,
        "daily_cap_usd": view.daily_cap_usd,
        "daily_cap_source": _cap_source(config, store, "daily_usd"),
        "daily_spent_usd": view.daily_spent_usd,
        "per_issue_remaining_usd": view.per_issue_remaining_usd,
        "daily_remaining_usd": view.daily_remaining_usd,
        "runtime_cap_exceeds_issue_headroom": view.runtime_cap_exceeds_issue_headroom,
        "source": "budget.effective_budget_view over recorded spend",
    }


def _warm_prefixes(store: Any, now: datetime) -> dict[str, set[str]]:
    warm: dict[str, set[str]] = {}
    for provider in ("anthropic", "openai", "google"):
        try:
            prefixes = store.warm_prefixes(provider, now)
        except Exception as exc:  # noqa: BLE001 — a cold read is a cold cache
            _logger.warning("dispatch plan: warm prefixes unreadable: %s", exc)
            continue
        if prefixes:
            warm[provider] = set(prefixes)
    return warm


def _no_advice(lane: _Lane, reason: str, source: str) -> dict[str, Any]:
    """The governor row when the governor had nothing to say about this route.

    Either it could not be evaluated or there is no route to evaluate; both
    recommend the route as resolved, which for an unresolvable one is ''.
    """
    return {
        "would_swap": False,
        "reason": reason,
        "recommended_runner": lane.runner,
        "recommended_model": lane.model,
        "est_savings_usd": 0.0,
        "source": source,
    }


def _governor(
    config: DispatchConfig,
    store: Any,
    lane: _Lane,
    complexity: str | None,
    now: datetime,
) -> dict[str, Any]:
    """Would the cache governor move this route, and why.

    Advice only: this endpoint writes no ``routing_decision`` row, because
    nothing is being dispatched. ``would_swap`` is the governor's ``applied``
    flag — what it would do to THIS route — and ``reason`` is its own code,
    shown beside a sentence rather than as one.

    A lane with no resolved route is not asked at all: the governor swaps one
    runner for another, and there is no proposal to swap.
    """
    from kew.runners.governor import PrefixKeyContext, cache_governor

    if lane.route is None:
        return _no_advice(lane, "no_route", "no route resolved, so no advice")
    try:
        _route, decision = cache_governor(
            proposed=lane.route,
            complexity=complexity,
            config=config,
            ctx=PrefixKeyContext(repo=config.project.repo or ""),
            warm_prefixes_by_provider=_warm_prefixes(store, now),
        )
    except Exception as exc:  # noqa: BLE001 — advice must never fail a reading
        _logger.warning("dispatch plan: governor unavailable: %s", exc)
        return _no_advice(lane, "governor_error", "the governor could not be evaluated")
    return {
        "would_swap": decision.applied,
        "reason": decision.reason,
        "recommended_runner": decision.recommended_runner,
        "recommended_model": decision.recommended_model,
        "est_savings_usd": decision.est_savings_usd,
        "source": "runners.governor.cache_governor (advice only, nothing written)",
    }


def _open_hold(store: Any, issue: int | None) -> bool | None:
    """Is an approval hold open on this item? ``None`` when it cannot be read."""
    if issue is None:
        return None
    from kew.report import open_approval_holds

    try:
        return len(open_approval_holds(store, issue_nums=[issue])) > 0
    except Exception as exc:  # noqa: BLE001 — an unreadable hold is not "no hold"
        _logger.warning("dispatch plan: approval holds unreadable: %s", exc)
        return None


def _headroom(budget: dict[str, Any]) -> dict[str, Any]:
    """The tightest money gate between here and a launch.

    The per-issue and daily remainders are the two the preflight checks, so the
    smaller of them is what a dispatch actually has to spend. ``None`` where
    spend is unknown — a dash, never a reassuring number.
    """
    per_issue = budget["per_issue_remaining_usd"]
    daily = budget["daily_remaining_usd"]
    known = [v for v in (per_issue, daily) if v is not None]
    if not known:
        return {"usd": None, "binds": None, "source": "spend could not be read"}
    binds = (
        "per_issue" if per_issue is not None and per_issue == min(known) else "daily"
    )
    return {
        "usd": min(known),
        "binds": binds,
        "source": f"[budget].{'per_issue_usd' if binds == 'per_issue' else 'daily_usd'}"
        " less recorded spend",
    }


def _admission(store: Any, issue: int | None, budget: dict[str, Any]) -> dict[str, Any]:
    """The three gates between a planned route and a real dispatch."""
    try:
        stopped = store.get_fleet_stopped()
    except Exception as exc:  # noqa: BLE001 — the latch read is never fatal
        _logger.warning("dispatch plan: fleet latch unreadable: %s", exc)
        stopped = None
    return {
        "fleet_stopped": stopped is not None,
        "fleet_stopped_reason": (stopped or {}).get("reason", "") if stopped else "",
        "open_hold": _open_hold(store, issue),
        "headroom": _headroom(budget),
        "source": "the fleet latch, the approval log and recorded spend",
    }


# ── the reading ─────────────────────────────────────────────────────────────


def _complexity(complexity: str | None, source: str) -> dict[str, Any]:
    """The tier and who named it, or an honest unknown.

    ``known: false`` is the flag the issue asks for: kew's own classifier reads
    the item's labels, and this endpoint makes no tracker call — so an absent
    complexity resolves ``[routing].default`` and says that it did, rather than
    guessing ``medium`` and presenting a route the dispatch may not take.
    """
    if complexity in TIERS:
        return {"value": complexity, "source": source, "known": True}
    return {
        "value": _UNKNOWN,
        "source": "no complexity given — the default tier is resolved",
        "known": False,
    }


def _tier(
    config: DispatchConfig, item_id: str | None, complexity: str | None
) -> tuple[str | None, str]:
    """``(tier, source)``: the caller's choice, else the ready queue's label.

    An item the queue holds with no tier label is ``medium``, not unknown:
    ``selector.get_complexity`` classifies an unlabelled item that way, so the
    default lane's route would be one the dispatch does not take. Only an item
    the queue does not hold stays unknown — its labels were never read.
    """
    if complexity in TIERS:
        return complexity, "the caller"
    queued = ready_queue_tier(config, item_id)
    if queued in TIERS:
        return queued, f"the ready queue's `{queued}` label"
    if queued == "":
        return (
            "medium",
            "the ready queue holds it with no tier label — dispatch reads that"
            " as `medium`",
        )
    return None, ""


def _item_keys(item: str | None) -> tuple[int | None, str | None]:
    """``(issue_num, item_id)`` from one item reference.

    A numeric reference is a GitHub issue and keys both dimensions; a tracker
    key (``SFD-12``) keys the per-item one only, which is what keeps it out of
    the ``0`` sentinel bucket (A3).

    "Numeric" is ASCII decimal and nothing else. ``str.isdigit`` is wider than
    ``int`` accepts — ``²`` passes it and raises a ValueError, a 500 out of a
    reader whose contract is that nothing here raises — and wider than a GitHub
    issue number is: ``٠`` (U+0660) is a decimal digit ``int`` reads as 0,
    which is the sentinel bucket this function exists to keep keys out of
    (reviewer P4).
    """
    if not item:
        return None, None
    text = item.strip().lstrip("#")
    if not text:
        return None, None
    if text.isascii() and text.isdigit():
        return int(text), text
    return None, text


def resolve_dispatch_plan(
    store: Any,
    config: DispatchConfig,
    *,
    item: str | None = None,
    complexity: str | None = None,
    lane: str = "implement",
    resolver: RouteResolver | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """One plan reading: what would run, and which rule decided each field.

    Reads only — no audit row, no reservation, no tracker call, no probe spend.
    An unresolvable route is reported through ``containment.refusal_reason``
    with ``posture: refused``; nothing here raises at a reader.
    """
    moment = now or datetime.now(UTC)
    route_cache = resolver if resolver is not None else RouteResolver()
    lane_name = lane if lane in LANES else "implement"
    issue, item_id = _item_keys(item)
    tier, tier_source = _tier(config, item_id, complexity)

    resolved_lane = _lane_route(config, lane_name, tier)
    resolved = route_cache.resolve(store, config, resolved_lane, tier)
    runner = resolved_lane.runner
    budget = _budget(config, store, issue, item_id)
    probe = resolved.probe
    return {
        "checked_at": moment.isoformat(),
        "item": item_id,
        "issue_num": issue,
        "lane": _sourced(lane_name, "the caller"),
        "complexity": _complexity(tier, tier_source),
        "runner": _sourced(
            runner,
            _override_source(store, resolved_lane.field, resolved_lane.runner_source),
        ),
        "model": _sourced(
            resolved_lane.model,
            _override_source(
                store, resolved_lane.model_field, resolved_lane.model_source
            ),
        ),
        "transport": _transport(resolved, config),
        "containment": _containment(resolved, config, runner, route_cache.cache),
        "budget": budget,
        "governor": _governor(config, store, resolved_lane, tier, moment),
        "route_fingerprint": _sourced(
            None if probe is None else probe.fingerprint,
            "probe_route.resolve_probe_route (spends nothing)",
        ),
        "live_status": _live_status(store, probe, moment),
        "admission": _admission(store, issue, budget),
        "command": _dispatch_command(lane_name, issue),
    }


def _dispatch_command(lane: str, issue: int | None) -> str | None:
    """The terminal command that dispatches this item, or ``None`` when none does.

    Composed here so a console never assembles a command line itself: it copies
    this string verbatim (#2517). Only the implement lane has a hand command
    (review and remediation are launched by the epic driver), and ``kew
    dispatch`` takes a GitHub issue number, so a tracker key gets none.
    """
    if lane != "implement" or issue is None:
        return None
    return f"kew dispatch {issue}"


def _unverified(source: str) -> dict[str, Any]:
    return {"value": _UNVERIFIED, "source": source, "probed_at": None}


def _probe_verdict(stored: Any, verdict: str) -> str:
    """``route_live_status``'s rule, over the strings the store persists.

    Fresh evidence answers ``confirmed`` or ``refuted`` by the probe's own
    settled state; anything else — expired, missed, a state this build has no
    word for — is ``unverified``. Never "probably fine because the static
    checks passed": that is the distinction the readiness slice exists to make.
    """
    if verdict != "hit" or stored is None:
        return _UNVERIFIED
    state = str(getattr(stored.state, "value", stored.state))
    return _CONFIRMED if state == "succeeded" else _REFUTED


def _live_status(store: Any, probe: Any | None, now: datetime) -> dict[str, Any]:
    """Stored live evidence for this exact boundary, and never a fresh probe.

    Read straight off the store, as ``service/work.py`` reads the same rows:
    this layer may not import ``kew.readiness_probe``, and a passive surface
    gets nothing but stored evidence anyway (``route_live_status``'s whole
    contract). ``unverified`` covers both "nobody confirmed this boundary" and
    "the evidence expired", which are the same thing to a reader deciding
    whether to trust the route.
    """
    if probe is None:
        return _unverified("no route to look evidence up for")
    lookup = getattr(store, "lookup_readiness_probe", None)
    if lookup is None:
        return _unverified("this ledger records no readiness probes")
    try:
        result = lookup(probe.fingerprint, now=now)
    except Exception as exc:  # noqa: BLE001 — unreadable evidence is unverified
        _logger.warning("dispatch plan: probe evidence unreadable: %s", exc)
        return _unverified("stored probe evidence could not be read")
    verdict = str(getattr(result.verdict, "value", result.verdict))
    stored = result.probe
    value = _probe_verdict(stored, verdict)
    settled = getattr(stored, "settled_at", None) if stored is not None else None
    return {
        "value": value,
        "source": (
            "stored readiness-probe evidence"
            if value != _UNVERIFIED
            else "no stored readiness probe for this boundary"
        ),
        "probed_at": None if settled is None else settled.isoformat(),
    }


__all__ = [
    "LANES",
    "ROUTE_TTL_S",
    "TIERS",
    "RouteResolver",
    "resolve_dispatch_plan",
]
