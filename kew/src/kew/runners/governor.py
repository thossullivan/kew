"""Cache-economics routing governor (Phase 2).

Pure function: ledger data + param table in, decision + rationale out.
Called after ``resolve_runner`` (stage 1) to optionally override the
proposed route with a warm, equal-or-stronger model when it saves money.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field

from kew.config import CacheConfig, DispatchConfig, RouteEntry, RoutingConfig
from kew.pricing import (
    ProviderCacheParams,
    compute_prefix_key,
    estimate_route_cost,
    get_provider_cache_params,
    infer_provider,
)
from kew.runners.registry import (
    ResolvedRoute,
    _get_runner_factories,
    _resolve_route_entry,
)

_logger = logging.getLogger(__name__)

_CAPABILITY_ORDER: dict[str, int] = {"simple": 0, "medium": 1, "complex": 2}
_CAPABILITY_TIERS = ("simple", "medium", "complex")

# Cheap char/token heuristic shared with repomap budgeting: ~4 chars/token.
_CHARS_PER_TOKEN = 4

# Fallback prompt size for callers without an assembled prompt (e.g. the
# --dry-run preview). The real dispatch path derives the estimate from the
# rendered prompt via ``estimate_prompt_tokens`` (spec D2 / R1-8).
DEFAULT_EST_PROMPT_TOKENS = 50_000


def estimate_prompt_tokens(rendered_prompt: str) -> int:
    """Estimate prompt tokens from the *actual* rendered prompt (spec D2 / R1-8).

    The governor's warm-vs-cold savings math scales linearly with prompt size,
    so feeding it the rendered prompt's token count — repo map, lesson corpus,
    and all injected context included — makes the estimate honest instead of the
    old hard-coded 50_000. Uses the same len//4 heuristic as repo-map budgeting;
    a 1-token floor keeps an empty prompt from zeroing the cost model.
    """
    return max(1, len(rendered_prompt) // _CHARS_PER_TOKEN)


@dataclass(frozen=True)
class CacheCandidate:
    """One candidate in the governor's evaluation set."""

    model: str
    runner_name: str
    provider: str
    warm: bool
    est_cost_usd: float
    capability: str


@dataclass(frozen=True)
class GovernorDecision:
    """The governor's evaluation result — always produced, even on passthrough."""

    proposed_model: str
    proposed_runner: str
    proposed_provider: str
    recommended_model: str
    recommended_runner: str
    recommended_provider: str
    candidates: list[CacheCandidate]
    warm_set: list[str] = field(default_factory=list)
    est_savings_usd: float = 0.0
    reason: str = "passthrough"
    applied: bool = False
    # Set only when containment refused the governed route and the proposed
    # route launched instead (reason ``containment_refused``).
    containment_refusal: str | None = None
    # Set only when an operator named the model for this one dispatch
    # (reason ``model_override``, #2741): the model, and where it was chosen.
    override: dict[str, str] | None = None

    def to_audit_payload(self) -> dict[str, object]:
        d = asdict(self)
        d["candidates"] = [asdict(c) for c in self.candidates]
        if self.containment_refusal is None:
            del d["containment_refusal"]
        if self.override is None:
            del d["override"]
        return d


@dataclass(frozen=True)
class PrefixKeyContext:
    """Stable inputs for prefix_key computation."""

    repo: str
    repo_map_hash: str = ""
    template_version: str = "1"


def _passthrough(
    proposed: ResolvedRoute,
    *,
    reason: str,
    candidates: list[CacheCandidate] | None = None,
) -> tuple[ResolvedRoute, GovernorDecision]:
    runner_name = getattr(proposed.runner, "name", "")
    provider = infer_provider(proposed.model, runner_name)
    return proposed, GovernorDecision(
        proposed_model=proposed.model,
        proposed_runner=runner_name,
        proposed_provider=provider,
        recommended_model=proposed.model,
        recommended_runner=runner_name,
        recommended_provider=provider,
        candidates=candidates or [],
        reason=reason,
        applied=False,
    )


def _make_warm_candidate(
    resolved: ResolvedRoute,
    capability: str,
    ctx: PrefixKeyContext,
    warm_prefixes_by_provider: dict[str, set[str]],
    est_prompt_tokens: int,
    cache_overrides: dict[str, ProviderCacheParams] | None,
) -> CacheCandidate | None:
    """Build a CacheCandidate if the resolved route is warm, else None."""
    rname = getattr(resolved.runner, "name", "")
    rprovider = infer_provider(resolved.model, rname)
    rpk = compute_prefix_key(
        repo=ctx.repo,
        runner=rname,
        model=resolved.model,
        provider=rprovider,
        repo_map_hash=ctx.repo_map_hash,
        template_version=ctx.template_version,
    )
    if rpk not in warm_prefixes_by_provider.get(rprovider, set()):
        return None
    return CacheCandidate(
        model=resolved.model,
        runner_name=rname,
        provider=rprovider,
        warm=True,
        est_cost_usd=estimate_route_cost(
            model=resolved.model,
            provider=rprovider,
            warm=True,
            est_prompt_tokens=est_prompt_tokens,
            cache_overrides=cache_overrides,
        ),
        capability=capability,
    )


def _collect_candidates(
    *,
    proposed: ResolvedRoute,
    complexity: str | None,
    routing: RoutingConfig,
    fallback_model: str,
    ctx: PrefixKeyContext,
    warm_prefixes_by_provider: dict[str, set[str]],
    est_prompt_tokens: int,
    cache_overrides: dict[str, ProviderCacheParams] | None,
) -> list[CacheCandidate]:
    """Build the candidate set: {proposed} ∪ {warm models at capability ≥ proposed}."""
    proposed_runner_name = getattr(proposed.runner, "name", "")
    proposed_provider = infer_provider(proposed.model, proposed_runner_name)
    proposed_cap = _CAPABILITY_ORDER.get(complexity or "simple", 0)

    proposed_pk = compute_prefix_key(
        repo=ctx.repo,
        runner=proposed_runner_name,
        model=proposed.model,
        provider=proposed_provider,
        repo_map_hash=ctx.repo_map_hash,
        template_version=ctx.template_version,
    )
    proposed_warm = proposed_pk in warm_prefixes_by_provider.get(
        proposed_provider, set()
    )

    candidates: list[CacheCandidate] = [
        CacheCandidate(
            model=proposed.model,
            runner_name=proposed_runner_name,
            provider=proposed_provider,
            warm=proposed_warm,
            est_cost_usd=estimate_route_cost(
                model=proposed.model,
                provider=proposed_provider,
                warm=proposed_warm,
                est_prompt_tokens=est_prompt_tokens,
                cache_overrides=cache_overrides,
            ),
            capability=complexity or "simple",
        )
    ]

    seen_models: set[str] = {proposed.model}
    entries = _tier_and_default_entries(routing, proposed_cap, fallback_model)

    for entry_resolved, cap in entries:
        if entry_resolved.model in seen_models:
            continue
        seen_models.add(entry_resolved.model)
        cand = _make_warm_candidate(
            entry_resolved,
            cap,
            ctx,
            warm_prefixes_by_provider,
            est_prompt_tokens,
            cache_overrides,
        )
        if cand is not None:
            candidates.append(cand)

    return candidates


def _tier_and_default_entries(
    routing: RoutingConfig,
    proposed_cap: int,
    fallback_model: str,
) -> list[tuple[ResolvedRoute, str]]:
    """Resolve routing-table entries at capability ≥ proposed + default."""
    runner_classes = _get_runner_factories()
    result: list[tuple[ResolvedRoute, str]] = []

    for tier in _CAPABILITY_TIERS:
        if _CAPABILITY_ORDER[tier] < proposed_cap:
            continue
        entry = getattr(routing, tier, None)
        if entry is None:
            continue
        try:
            resolved = _resolve_route_entry(entry, runner_classes, fallback_model)
        except (ValueError, KeyError):
            continue
        result.append((resolved, tier))

    if _CAPABILITY_ORDER["medium"] >= proposed_cap:
        try:
            default = _resolve_route_entry(
                routing.default,
                runner_classes,
                fallback_model,
            )
            result.append((default, "medium"))
        except (ValueError, KeyError):
            pass

    return result


def _pick_override_route(
    best: CacheCandidate,
    proposed: ResolvedRoute,
    config: DispatchConfig,
    candidates: list[CacheCandidate],
    cache_aware: bool,
) -> tuple[ResolvedRoute, str | None]:
    """Resolve the override route if cache_aware, else return proposed."""
    if not cache_aware:
        return proposed, None
    try:
        entry = RouteEntry(runner=best.runner_name, model=best.model)
        return _resolve_route_entry(
            entry,
            _get_runner_factories(),
            config.agent.model,
        ), None
    except (ValueError, KeyError):
        _logger.warning(
            "Governor override failed to resolve runner for %s — passthrough",
            best.model,
        )
        return proposed, "override_resolve_failed"


def cache_governor(
    *,
    proposed: ResolvedRoute,
    complexity: str | None,
    config: DispatchConfig,
    ctx: PrefixKeyContext,
    warm_prefixes_by_provider: dict[str, set[str]],
    # Callers on the real dispatch path derive this from the rendered prompt via
    # ``estimate_prompt_tokens`` (spec D2 / R1-8). The default is only a fallback
    # for callers without an assembled prompt (e.g. dry-run advice).
    est_prompt_tokens: int = DEFAULT_EST_PROMPT_TOKENS,
    model_pinned: bool = False,
    cache_overrides: dict[str, ProviderCacheParams] | None = None,
) -> tuple[ResolvedRoute, GovernorDecision]:
    """Evaluate whether a warm, equal-or-stronger model beats proposed."""
    if model_pinned:
        return _passthrough(proposed, reason="model_pinned")

    routing = config.routing
    if routing is None:
        return _passthrough(proposed, reason="no_routing_config")

    proposed_runner_name = getattr(proposed.runner, "name", "")
    proposed_provider = infer_provider(proposed.model, proposed_runner_name)

    params = get_provider_cache_params(proposed_provider, cache_overrides)
    if not params.caches or params.read_mult >= 1.0:
        return _passthrough(proposed, reason="provider_no_cache_benefit")

    candidates = _collect_candidates(
        proposed=proposed,
        complexity=complexity,
        routing=routing,
        fallback_model=config.agent.model,
        ctx=ctx,
        warm_prefixes_by_provider=warm_prefixes_by_provider,
        est_prompt_tokens=est_prompt_tokens,
        cache_overrides=cache_overrides,
    )

    warm_alternatives = [c for c in candidates[1:] if c.warm]
    if not warm_alternatives:
        return _passthrough(
            proposed, reason="no_warm_alternatives", candidates=candidates
        )

    return _evaluate_candidates(
        proposed,
        candidates,
        warm_prefixes_by_provider,
        routing.cache or CacheConfig(),
        routing.cache_aware,
        config,
        proposed_runner_name,
        proposed_provider,
    )


def _evaluate_candidates(
    proposed: ResolvedRoute,
    candidates: list[CacheCandidate],
    warm_prefixes_by_provider: dict[str, set[str]],
    cache_cfg: CacheConfig,
    cache_aware: bool,
    config: DispatchConfig,
    proposed_runner_name: str,
    proposed_provider: str,
) -> tuple[ResolvedRoute, GovernorDecision]:
    """Compare candidates, apply threshold, and build the decision."""
    proposed_candidate = candidates[0]
    proposed_cap = _CAPABILITY_ORDER.get(proposed_candidate.capability, 0)
    eligible = [
        c for c in candidates if _CAPABILITY_ORDER.get(c.capability, 0) >= proposed_cap
    ]
    best = min(eligible, key=lambda c: c.est_cost_usd)

    all_warm_pks: list[str] = []
    for _provider, pks in warm_prefixes_by_provider.items():
        all_warm_pks.extend(pks)

    if best.model == proposed_candidate.model:
        return proposed, GovernorDecision(
            proposed_model=proposed.model,
            proposed_runner=proposed_runner_name,
            proposed_provider=proposed_provider,
            recommended_model=proposed.model,
            recommended_runner=proposed_runner_name,
            recommended_provider=proposed_provider,
            candidates=candidates,
            warm_set=all_warm_pks,
            reason="proposed_already_cheapest",
            applied=False,
        )

    savings = proposed_candidate.est_cost_usd - best.est_cost_usd

    if savings <= cache_cfg.governor_threshold_usd:
        return proposed, GovernorDecision(
            proposed_model=proposed.model,
            proposed_runner=proposed_runner_name,
            proposed_provider=proposed_provider,
            recommended_model=best.model,
            recommended_runner=best.runner_name,
            recommended_provider=best.provider,
            candidates=candidates,
            warm_set=all_warm_pks,
            est_savings_usd=savings,
            reason="savings_below_threshold",
            applied=False,
        )

    override_route, err = _pick_override_route(
        best,
        proposed,
        config,
        candidates,
        cache_aware,
    )
    if err:
        return _passthrough(proposed, reason=err, candidates=candidates)

    return override_route, GovernorDecision(
        proposed_model=proposed.model,
        proposed_runner=proposed_runner_name,
        proposed_provider=proposed_provider,
        recommended_model=best.model,
        recommended_runner=best.runner_name,
        recommended_provider=best.provider,
        candidates=candidates,
        warm_set=all_warm_pks,
        est_savings_usd=savings,
        reason="warm_stronger_beats_cold",
        applied=cache_aware,
    )


def _candidate_cost(
    decision: GovernorDecision,
    model: str,
) -> float:
    """Look up a candidate's estimated cost by model name."""
    for c in decision.candidates:
        if c.model == model:
            return c.est_cost_usd
    return 0.0


def _format_skip(decision: GovernorDecision) -> str | None:
    """Format simple skip/passthrough reasons; None if not a skip."""
    r = decision.reason
    if r == "model_pinned":
        return f"  Governor: skipped (model pinned to {decision.proposed_model})"
    if r == "no_routing_config":
        return "  Governor: skipped (no routing config)"
    if r == "provider_no_cache_benefit":
        return (
            f"  Governor: skipped (provider {decision.proposed_provider!r}"
            " does not benefit from caching)"
        )
    if r == "savings_below_threshold":
        return (
            f"  Governor: {decision.recommended_model} warm"
            f" (${decision.est_savings_usd:.4f} savings below threshold)"
        )
    return None


def format_governor_reasoning(decision: GovernorDecision) -> str:
    """Human-readable reasoning for --dry-run / advise-mode output."""
    skip = _format_skip(decision)
    if skip is not None:
        return skip

    cost_str = ""
    if decision.candidates:
        cost_str = f" (cold, ${decision.candidates[0].est_cost_usd:.4f})"

    if decision.reason == "no_warm_alternatives":
        model = decision.proposed_model
        return f"  Governor: no warm alternatives for {model}{cost_str}"

    if decision.reason == "proposed_already_cheapest":
        cost_str = ""
        if decision.candidates:
            cost_str = f" (${decision.candidates[0].est_cost_usd:.4f})"
        return f"  Governor: {decision.proposed_model} already cheapest{cost_str}"

    proposed_cost = _candidate_cost(decision, decision.proposed_model)
    rec_cost = _candidate_cost(decision, decision.recommended_model)
    verb = "overriding" if decision.applied else "recommend"
    return (
        f"  Governor: proposed {decision.proposed_model}"
        f" (cold, ${proposed_cost:.4f});"
        f" {decision.recommended_model} warm"
        f" (${rec_cost:.4f})"
        f" → {verb} {decision.recommended_model},"
        f" save ${decision.est_savings_usd:.4f}"
    )
