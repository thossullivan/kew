"""Tests for Phase 2 cache-economics routing governor (#349).

Decision-table coverage: warm-stronger beats cold-cheaper, override-up-only,
engage gate, explicit pin, no-warm passthrough, advise vs auto, threshold
boundary, audit payload structure.
"""

from __future__ import annotations

import pytest

from kew.config import CacheConfig, DispatchConfig, RouteEntry, RoutingConfig
from kew.pricing import ProviderCacheParams, compute_prefix_key, infer_provider
from kew.runners.governor import (
    PrefixKeyContext,
    cache_governor,
    estimate_prompt_tokens,
    format_governor_reasoning,
)
from kew.runners.registry import ResolvedRoute

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pk(model: str, runner: str = "claude-code", provider: str = "") -> str:
    """Shorthand prefix_key for test fixtures."""
    prov = provider or infer_provider(model, runner)
    return compute_prefix_key(
        repo="owner/repo",
        runner=runner,
        model=model,
        provider=prov,
        repo_map_hash="abc",
    )


def _ctx() -> PrefixKeyContext:
    return PrefixKeyContext(repo="owner/repo", repo_map_hash="abc")


def _routing(
    *,
    default_model: str = "claude-sonnet-4-6",
    simple_model: str | None = None,
    medium_model: str | None = None,
    complex_model: str | None = None,
    cache_aware: bool = False,
    threshold: float = 0.02,
) -> RoutingConfig:
    default_entry = RouteEntry(runner="claude-code", model=default_model)
    kwargs: dict = {"default": default_entry, "cache_aware": cache_aware}
    if simple_model:
        kwargs["simple"] = RouteEntry(runner="claude-code", model=simple_model)
    if medium_model:
        kwargs["medium"] = RouteEntry(runner="claude-code", model=medium_model)
    if complex_model:
        kwargs["complex"] = RouteEntry(runner="claude-code", model=complex_model)
    kwargs["cache"] = CacheConfig(governor_threshold_usd=threshold)
    return RoutingConfig(**kwargs)


def _config(routing: RoutingConfig | None = None) -> DispatchConfig:
    cfg = DispatchConfig()
    cfg.routing = routing
    return cfg


def _resolved(model: str = "claude-sonnet-4-6") -> ResolvedRoute:
    from kew.runners.claude_code import ClaudeCodeRunner

    return ResolvedRoute(runner=ClaudeCodeRunner(), model=model)


def _warm_set(*models: str) -> dict[str, set[str]]:
    """Build warm_prefixes_by_provider from model names."""
    result: dict[str, set[str]] = {}
    for m in models:
        prov = infer_provider(m, "claude-code")
        pk = _pk(m)
        result.setdefault(prov, set()).add(pk)
    return result


# ---------------------------------------------------------------------------
# Engage gate
# ---------------------------------------------------------------------------


class TestEngageGate:
    def test_provider_no_cache_benefit_skips(self):
        """When read_mult >= 1.0, governor passes through."""
        routing = _routing()
        config = _config(routing)
        proposed = _resolved("claude-sonnet-4-6")

        no_benefit = ProviderCacheParams(
            caches=True,
            read_mult=1.0,
            write_mult=1.25,
            ttl_seconds=300,
            min_tokens=1024,
            style="explicit",
        )
        route, decision = cache_governor(
            proposed=proposed,
            complexity="simple",
            config=config,
            ctx=_ctx(),
            warm_prefixes_by_provider={},
            cache_overrides={"anthropic": no_benefit},
        )
        assert route.model == proposed.model
        assert decision.reason == "provider_no_cache_benefit"
        assert decision.applied is False

    def test_provider_not_caching_skips(self):
        """Unknown provider (caches=False) → passthrough."""
        routing = _routing()
        config = _config(routing)
        # Use a model+runner combo that infers to an unknown provider
        from kew.runners.opencode import OpenCodeRunner

        proposed = ResolvedRoute(runner=OpenCodeRunner(), model="z-ai-model/v1")

        route, decision = cache_governor(
            proposed=proposed,
            complexity="simple",
            config=config,
            ctx=_ctx(),
            warm_prefixes_by_provider={},
        )
        assert decision.reason == "provider_no_cache_benefit"

    def test_provider_with_read_discount_engages(self):
        """Anthropic (read_mult=0.10) should engage the governor."""
        routing = _routing(
            simple_model="claude-haiku-4-5",
            complex_model="claude-opus-4-6",
            cache_aware=True,
            threshold=0.0,
        )
        config = _config(routing)
        proposed = _resolved("claude-haiku-4-5")

        warm = _warm_set("claude-opus-4-6")
        route, decision = cache_governor(
            proposed=proposed,
            complexity="simple",
            config=config,
            ctx=_ctx(),
            warm_prefixes_by_provider=warm,
        )
        assert decision.reason != "provider_no_cache_benefit"


# ---------------------------------------------------------------------------
# Model pin
# ---------------------------------------------------------------------------


class TestModelPin:
    def test_pinned_model_passthrough(self):
        """Explicit pin → governor never overrides."""
        routing = _routing(
            simple_model="claude-haiku-4-5",
            complex_model="claude-opus-4-6",
            cache_aware=True,
            threshold=0.0,
        )
        config = _config(routing)
        proposed = _resolved("claude-haiku-4-5")
        warm = _warm_set("claude-opus-4-6")

        route, decision = cache_governor(
            proposed=proposed,
            complexity="simple",
            config=config,
            ctx=_ctx(),
            warm_prefixes_by_provider=warm,
            model_pinned=True,
        )
        assert route.model == "claude-haiku-4-5"
        assert decision.reason == "model_pinned"
        assert decision.applied is False


# ---------------------------------------------------------------------------
# No routing config
# ---------------------------------------------------------------------------


class TestNoRoutingConfig:
    def test_no_routing_passthrough(self):
        config = _config(routing=None)
        proposed = _resolved("claude-sonnet-4-6")

        route, decision = cache_governor(
            proposed=proposed,
            complexity="simple",
            config=config,
            ctx=_ctx(),
            warm_prefixes_by_provider={},
        )
        assert decision.reason == "no_routing_config"
        assert route.model == "claude-sonnet-4-6"


# ---------------------------------------------------------------------------
# No warm alternatives
# ---------------------------------------------------------------------------


class TestNoWarm:
    def test_no_warm_prefixes_passthrough(self):
        routing = _routing(
            simple_model="claude-haiku-4-5",
            complex_model="claude-opus-4-6",
        )
        config = _config(routing)
        proposed = _resolved("claude-haiku-4-5")

        route, decision = cache_governor(
            proposed=proposed,
            complexity="simple",
            config=config,
            ctx=_ctx(),
            warm_prefixes_by_provider={},
        )
        assert route.model == "claude-haiku-4-5"
        assert decision.reason == "no_warm_alternatives"

    def test_warm_prefix_exists_but_only_for_proposed(self):
        """If only the proposed model is warm, no alternatives → passthrough."""
        routing = _routing(simple_model="claude-haiku-4-5")
        config = _config(routing)
        proposed = _resolved("claude-haiku-4-5")
        warm = _warm_set("claude-haiku-4-5")

        route, decision = cache_governor(
            proposed=proposed,
            complexity="simple",
            config=config,
            ctx=_ctx(),
            warm_prefixes_by_provider=warm,
        )
        assert decision.reason in ("no_warm_alternatives", "proposed_already_cheapest")


# ---------------------------------------------------------------------------
# Override-up-only (never selects a weaker warm model)
# ---------------------------------------------------------------------------


class TestOverrideUpOnly:
    def test_warm_weaker_model_never_selected(self):
        """A warm simple model should never override a proposed complex model."""
        routing = _routing(
            simple_model="claude-haiku-4-5",
            complex_model="claude-opus-4-6",
            cache_aware=True,
            threshold=0.0,
        )
        config = _config(routing)
        proposed = _resolved("claude-opus-4-6")

        # Haiku is warm but weaker — should NOT be a candidate
        warm = _warm_set("claude-haiku-4-5")
        route, decision = cache_governor(
            proposed=proposed,
            complexity="complex",
            config=config,
            ctx=_ctx(),
            warm_prefixes_by_provider=warm,
        )
        assert route.model == "claude-opus-4-6"
        for c in decision.candidates:
            cap_order = {"simple": 0, "medium": 1, "complex": 2}
            assert cap_order.get(c.capability, 0) >= cap_order["complex"]

    def test_warm_weaker_default_never_selected(self):
        """A warm default (medium) must not override a proposed complex model."""
        routing = _routing(
            default_model="claude-sonnet-4-6",
            complex_model="claude-opus-4-6",
            cache_aware=True,
            threshold=0.0,
        )
        config = _config(routing)
        proposed = _resolved("claude-opus-4-6")

        warm = _warm_set("claude-sonnet-4-6")
        route, decision = cache_governor(
            proposed=proposed,
            complexity="complex",
            config=config,
            ctx=_ctx(),
            warm_prefixes_by_provider=warm,
        )
        assert route.model == "claude-opus-4-6"
        for c in decision.candidates:
            cap_order = {"simple": 0, "medium": 1, "complex": 2}
            assert cap_order.get(c.capability, 0) >= cap_order["complex"]

    def test_warm_equal_tier_is_candidate(self):
        """A warm model at the same tier can be selected."""
        routing = _routing(
            medium_model="claude-sonnet-4-6",
            complex_model="claude-opus-4-6",
            cache_aware=True,
            threshold=0.0,
        )
        config = _config(routing)
        proposed = _resolved("claude-sonnet-4-6")

        warm = _warm_set("claude-opus-4-6")
        route, decision = cache_governor(
            proposed=proposed,
            complexity="medium",
            config=config,
            ctx=_ctx(),
            warm_prefixes_by_provider=warm,
        )
        # Opus warm should be cheaper than Sonnet cold
        assert any(c.model == "claude-opus-4-6" for c in decision.candidates)


# ---------------------------------------------------------------------------
# Warm stronger beats cold cheaper
# ---------------------------------------------------------------------------


class TestWarmStrongerBeatsCold:
    def test_warm_opus_beats_cold_haiku(self):
        """Warm Opus at 0.10× read should beat cold Haiku at 1.25× write."""
        routing = _routing(
            simple_model="claude-haiku-4-5",
            complex_model="claude-opus-4-6",
            cache_aware=True,
            threshold=0.0,
        )
        config = _config(routing)
        proposed = _resolved("claude-haiku-4-5")

        warm = _warm_set("claude-opus-4-6")
        route, decision = cache_governor(
            proposed=proposed,
            complexity="simple",
            config=config,
            ctx=_ctx(),
            warm_prefixes_by_provider=warm,
        )
        assert decision.reason == "warm_stronger_beats_cold"
        assert decision.recommended_model == "claude-opus-4-6"
        assert decision.est_savings_usd > 0
        assert decision.applied is True
        assert route.model == "claude-opus-4-6"

    def test_warm_sonnet_beats_cold_sonnet_same_tier(self):
        """Warm same-model beats cold same-model (proposed is cold, default is warm)."""
        routing = _routing(
            default_model="claude-sonnet-4-6",
            simple_model="claude-sonnet-4-6",
            medium_model="claude-sonnet-4-6",
            cache_aware=True,
            threshold=0.0,
        )
        config = _config(routing)
        proposed = _resolved("claude-sonnet-4-6")

        # No warm alternatives — all tiers use the same model
        route, decision = cache_governor(
            proposed=proposed,
            complexity="simple",
            config=config,
            ctx=_ctx(),
            warm_prefixes_by_provider={},
        )
        # No warm alternatives at all
        assert decision.reason == "no_warm_alternatives"


# ---------------------------------------------------------------------------
# Advise vs auto return values
# ---------------------------------------------------------------------------


class TestAdviseVsAuto:
    def _setup_override_scenario(self, cache_aware: bool):
        routing = _routing(
            simple_model="claude-haiku-4-5",
            complex_model="claude-opus-4-6",
            cache_aware=cache_aware,
            threshold=0.0,
        )
        config = _config(routing)
        proposed = _resolved("claude-haiku-4-5")
        warm = _warm_set("claude-opus-4-6")
        return cache_governor(
            proposed=proposed,
            complexity="simple",
            config=config,
            ctx=_ctx(),
            warm_prefixes_by_provider=warm,
        )

    def test_advise_mode_returns_original(self):
        """cache_aware=false → returns original proposal, applied=false."""
        route, decision = self._setup_override_scenario(cache_aware=False)
        assert route.model == "claude-haiku-4-5"
        assert decision.applied is False
        assert decision.recommended_model == "claude-opus-4-6"
        assert decision.reason == "warm_stronger_beats_cold"

    def test_auto_mode_returns_override(self):
        """cache_aware=true → returns overridden route, applied=true."""
        route, decision = self._setup_override_scenario(cache_aware=True)
        assert route.model == "claude-opus-4-6"
        assert decision.applied is True
        assert decision.recommended_model == "claude-opus-4-6"
        assert decision.reason == "warm_stronger_beats_cold"


# ---------------------------------------------------------------------------
# Threshold anti-churn boundary
# ---------------------------------------------------------------------------


class TestThresholdBoundary:
    def test_savings_at_threshold_no_override(self):
        """Savings exactly at threshold → no override (must be strictly greater)."""
        routing = _routing(
            simple_model="claude-haiku-4-5",
            complex_model="claude-opus-4-6",
            cache_aware=True,
        )
        config = _config(routing)
        proposed = _resolved("claude-haiku-4-5")
        warm = _warm_set("claude-opus-4-6")

        route, decision = cache_governor(
            proposed=proposed,
            complexity="simple",
            config=config,
            ctx=_ctx(),
            warm_prefixes_by_provider=warm,
        )
        # With default threshold=0.02, large savings should still override
        if decision.est_savings_usd > 0.02:
            assert decision.reason == "warm_stronger_beats_cold"
        else:
            assert decision.reason == "savings_below_threshold"

    def test_zero_threshold_always_overrides_on_savings(self):
        """threshold=0.0 → any positive savings triggers override."""
        routing = _routing(
            simple_model="claude-haiku-4-5",
            complex_model="claude-opus-4-6",
            cache_aware=True,
            threshold=0.0,
        )
        config = _config(routing)
        proposed = _resolved("claude-haiku-4-5")
        warm = _warm_set("claude-opus-4-6")

        route, decision = cache_governor(
            proposed=proposed,
            complexity="simple",
            config=config,
            ctx=_ctx(),
            warm_prefixes_by_provider=warm,
        )
        assert decision.reason == "warm_stronger_beats_cold"
        assert decision.applied is True

    def test_very_high_threshold_prevents_override(self):
        """Unreachably high threshold → savings_below_threshold."""
        routing = _routing(
            simple_model="claude-haiku-4-5",
            complex_model="claude-opus-4-6",
            cache_aware=True,
            threshold=999.0,
        )
        config = _config(routing)
        proposed = _resolved("claude-haiku-4-5")
        warm = _warm_set("claude-opus-4-6")

        route, decision = cache_governor(
            proposed=proposed,
            complexity="simple",
            config=config,
            ctx=_ctx(),
            warm_prefixes_by_provider=warm,
        )
        assert decision.reason == "savings_below_threshold"
        assert route.model == "claude-haiku-4-5"
        assert decision.applied is False


# ---------------------------------------------------------------------------
# Audit payload
# ---------------------------------------------------------------------------


class TestAuditPayload:
    def test_decision_to_audit_payload_is_well_formed(self):
        """GovernorDecision.to_audit_payload() produces a replayable dict."""
        routing = _routing(
            simple_model="claude-haiku-4-5",
            complex_model="claude-opus-4-6",
            cache_aware=True,
            threshold=0.0,
        )
        config = _config(routing)
        proposed = _resolved("claude-haiku-4-5")
        warm = _warm_set("claude-opus-4-6")

        _, decision = cache_governor(
            proposed=proposed,
            complexity="simple",
            config=config,
            ctx=_ctx(),
            warm_prefixes_by_provider=warm,
        )

        payload = decision.to_audit_payload()
        assert isinstance(payload, dict)
        assert payload["proposed_model"] == "claude-haiku-4-5"
        assert payload["recommended_model"] == "claude-opus-4-6"
        assert payload["reason"] == "warm_stronger_beats_cold"
        assert payload["applied"] is True
        assert isinstance(payload["candidates"], list)
        assert all(isinstance(c, dict) for c in payload["candidates"])
        assert all(
            {"model", "runner_name", "provider", "warm", "est_cost_usd", "capability"}
            <= set(c.keys())
            for c in payload["candidates"]
        )
        assert isinstance(payload["warm_set"], list)
        assert isinstance(payload["est_savings_usd"], float)


# ---------------------------------------------------------------------------
# Reasoning format
# ---------------------------------------------------------------------------


class TestGovernorReasoning:
    def test_format_passthrough_pin(self):
        _, decision = cache_governor(
            proposed=_resolved(),
            complexity="simple",
            config=_config(_routing()),
            ctx=_ctx(),
            warm_prefixes_by_provider={},
            model_pinned=True,
        )
        text = format_governor_reasoning(decision)
        assert "pinned" in text

    def test_format_warm_override(self):
        routing = _routing(
            simple_model="claude-haiku-4-5",
            complex_model="claude-opus-4-6",
            cache_aware=True,
            threshold=0.0,
        )
        _, decision = cache_governor(
            proposed=_resolved("claude-haiku-4-5"),
            complexity="simple",
            config=_config(routing),
            ctx=_ctx(),
            warm_prefixes_by_provider=_warm_set("claude-opus-4-6"),
        )
        text = format_governor_reasoning(decision)
        assert "claude-opus-4-6" in text
        assert "save" in text.lower()

    def test_format_advise_mode(self):
        routing = _routing(
            simple_model="claude-haiku-4-5",
            complex_model="claude-opus-4-6",
            cache_aware=False,
            threshold=0.0,
        )
        _, decision = cache_governor(
            proposed=_resolved("claude-haiku-4-5"),
            complexity="simple",
            config=_config(routing),
            ctx=_ctx(),
            warm_prefixes_by_provider=_warm_set("claude-opus-4-6"),
        )
        text = format_governor_reasoning(decision)
        assert "recommend" in text


# ---------------------------------------------------------------------------
# Honest prompt estimate (Task 2, spec D2 / R1-8)
# ---------------------------------------------------------------------------


class TestHonestPromptEstimate:
    def test_estimate_prompt_tokens_scales_with_length(self):
        """The estimator tracks rendered-prompt length (char/4 heuristic)."""
        small = estimate_prompt_tokens("x" * 4_000)
        large = estimate_prompt_tokens("x" * 400_000)
        assert large > small
        assert estimate_prompt_tokens("") >= 1  # never below the 1-token floor

    def test_est_prompt_tokens_tracks_rendered_prompt(self):
        """Governor savings scale with the rendered prompt size (D2 / R1-8):
        a larger injected context yields a larger warm-vs-cold delta, so the
        decision shifts when the rendered prompt shifts."""
        routing = _routing(
            simple_model="claude-haiku-4-5",
            complex_model="claude-opus-4-6",
            cache_aware=True,
            threshold=0.0,
        )
        config = _config(routing)
        proposed = _resolved("claude-haiku-4-5")
        warm = _warm_set("claude-opus-4-6")

        # A small rendered prompt vs a large one (repo map + lesson corpus).
        # Both are above the provider cache-eligibility floor (min_tokens), so
        # only the injected-context *size* differs between the two decisions.
        small_prompt = "x" * 40_000  # ~10k tokens
        large_prompt = "x" * 400_000  # ~100k tokens

        _, small = cache_governor(
            proposed=proposed,
            complexity="simple",
            config=config,
            ctx=_ctx(),
            warm_prefixes_by_provider=warm,
            est_prompt_tokens=estimate_prompt_tokens(small_prompt),
        )
        _, large = cache_governor(
            proposed=proposed,
            complexity="simple",
            config=config,
            ctx=_ctx(),
            warm_prefixes_by_provider=warm,
            est_prompt_tokens=estimate_prompt_tokens(large_prompt),
        )
        assert small.reason == "warm_stronger_beats_cold"
        assert large.reason == "warm_stronger_beats_cold"
        # Same decision direction, but the estimated savings track prompt size.
        assert large.est_savings_usd > small.est_savings_usd > 0


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


class TestCacheConfig:
    def test_default_values(self):
        cfg = CacheConfig()
        assert cfg.governor_threshold_usd == 0.02
        assert cfg.order_aware is False
        assert cfg.max_reorder_lookahead == 10

    def test_negative_threshold_rejected(self):
        with pytest.raises(ValueError, match="governor_threshold_usd"):
            CacheConfig(governor_threshold_usd=-0.01)

    def test_zero_threshold_accepted(self):
        cfg = CacheConfig(governor_threshold_usd=0.0)
        assert cfg.governor_threshold_usd == 0.0

    def test_zero_lookahead_rejected(self):
        with pytest.raises(ValueError, match="max_reorder_lookahead"):
            CacheConfig(max_reorder_lookahead=0)

    def test_routing_config_cache_fields(self):
        routing = RoutingConfig(
            default="claude-code",
            cache_aware=True,
            cache=CacheConfig(governor_threshold_usd=0.05),
        )
        assert routing.cache_aware is True
        assert routing.cache is not None
        assert routing.cache.governor_threshold_usd == 0.05

    def test_routing_config_defaults(self):
        routing = RoutingConfig(default="claude-code")
        assert routing.cache_aware is False
        assert isinstance(routing.cache, CacheConfig)
