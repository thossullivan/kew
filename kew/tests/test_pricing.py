"""Tests for the pricing module."""

import warnings

import pytest

from kew.pricing import (
    CACHE_READ_MULTIPLIER,
    CACHE_WRITE_MULTIPLIER,
    UnknownModelCostError,
    build_pricing_overrides,
    catalog_pricing,
    compute_cost_from_tokens,
    estimate_cost,
    estimate_route_cost,
    get_model_cache_params,
    require_pricing_key,
    resolve_pricing_key,
)

PRICES = catalog_pricing()


def test_gpt_5_6_sol_uses_conservative_long_context_price_and_cache():
    assert PRICES["gpt-5.6-sol"] == (10.0, 45.0)
    cost = estimate_cost(
        "gpt-5.6-sol",
        input_tokens=100,
        cache_read_tokens=100,
        output_tokens=100,
    )
    assert cost == pytest.approx(
        (100 * 10.0 + 100 * 10.0 * 0.10 + 100 * 45.0) / 1_000_000
    )


@pytest.mark.parametrize(
    ("model", "price"),
    [
        ("claude-opus-5-5", (4.0, 20.0)),
        ("claude-sonnet-5-5", (2.0, 10.0)),
        ("claude-sonnet-5", (2.0, 10.0)),
    ],
)
def test_the_5_5_generation_and_sonnet_5_carry_their_list_prices(model, price):
    """Pricing page, 2026-10-08. Each id resolves to its OWN key: before its
    row existed, claude-opus-5-5 rode claude-opus-5 by dash prefix at $5/$25.
    Prefix matching is gone (#2709), so an undeclared dated id has no price."""
    assert resolve_pricing_key(model, PRICES) == model
    assert resolve_pricing_key(model + "-20261008", PRICES) is None
    assert PRICES[model] == price


@pytest.mark.parametrize("model", ["claude-opus-5-5", "claude-sonnet-5-5"])
def test_the_5_5_generation_reads_the_cache_at_five_percent(model):
    params = get_model_cache_params(model, "anthropic")
    assert params.read_mult == 0.05
    assert params.write_mult == 1.25
    input_price, output_price = PRICES[model]
    cost = estimate_cost(
        model, input_tokens=100, cache_read_tokens=100, output_tokens=100
    )
    assert cost == pytest.approx(
        (100 * input_price + 100 * input_price * 0.05 + 100 * output_price) / 1_000_000
    )


def test_opus_5_keeps_the_standard_cache_read():
    assert get_model_cache_params("claude-opus-5", "anthropic").read_mult == 0.10


def test_gpt_5_6_luna_uses_conservative_long_context_price_and_cache():
    assert PRICES["gpt-5.6-luna"] == (0.40, 1.80)
    cost = estimate_cost(
        "gpt-5.6-luna",
        input_tokens=100,
        cache_read_tokens=100,
        output_tokens=100,
    )
    assert cost == pytest.approx(
        (100 * 0.40 + 100 * 0.40 * 0.10 + 100 * 1.80) / 1_000_000
    )
    params = get_model_cache_params("gpt-5.6-luna", "openai")
    assert params.read_mult == 0.10
    assert params.write_mult == 1.25


def test_codex_cache_rate_does_not_change_other_openai_models():
    assert get_model_cache_params("gpt-5.6-sol", "openai").read_mult == 0.10
    # No prefix ride (#2709): an undeclared dated id reads the provider default.
    suffixed = get_model_cache_params("gpt-5.6-sol-20260721", "openai")
    assert suffixed.read_mult == 0.50
    assert get_model_cache_params("gpt-5.6-sol", "openai").write_mult == 1.25
    assert get_model_cache_params("gpt-other", "openai").read_mult == 0.50


def test_catalog_cache_ttl_overrides_the_provider_default():
    # #2714: Luna's catalog row keeps its 1800 s warmth window; Sol's row
    # records no TTL, so it keeps the OpenAI provider default.
    assert get_model_cache_params("gpt-5.6-luna", "openai").ttl_seconds == 1800
    assert get_model_cache_params("gpt-5.6-sol", "openai").ttl_seconds == 600


def test_codex_cold_route_uses_cache_creation_premium():
    cost = estimate_route_cost(
        model="gpt-5.6-sol",
        provider="openai",
        warm=False,
        est_prompt_tokens=1024,
    )
    assert cost == pytest.approx(1024 * 10.0 * 1.25 / 1_000_000)


def test_model_pricing_has_known_models():
    """PRICES should contain pricing for common Claude models."""
    assert "claude-sonnet-4" in PRICES
    assert "claude-opus-4" in PRICES
    assert "claude-haiku-4" in PRICES


def test_model_pricing_has_newer_claude_models():
    """PRICES should explicitly list Claude 4.5/4.6 variants."""
    assert "claude-sonnet-4-5" in PRICES
    assert "claude-sonnet-4-6" in PRICES
    assert "claude-haiku-4-5" in PRICES
    assert "claude-opus-4-5" in PRICES
    assert "claude-opus-4-6" in PRICES


def test_newer_models_have_valid_pricing():
    """Newer model entries should have positive input and output pricing."""
    for model in (
        "claude-sonnet-4-5",
        "claude-sonnet-4-6",
        "claude-haiku-4-5",
        "claude-opus-4-5",
        "claude-opus-4-6",
    ):
        input_cost, output_cost = PRICES[model]
        assert input_cost > 0, f"{model} input cost must be positive"
        assert output_cost > 0, f"{model} output cost must be positive"


def test_model_pricing_has_claude_fable_5():
    """claude-fable-5 is priced at $10/M input, $50/M output (launched 2026-06-09)."""
    assert PRICES["claude-fable-5"] == (10.0, 50.0)
    cost = estimate_cost(
        "claude-fable-5", input_tokens=1_000_000, output_tokens=1_000_000
    )
    assert cost == 60.0


def test_opus_4_base_prices_only_itself():
    """`claude-opus-4` lists at $15/$75 and prices only that exact id.

    Opus 4.5 onward list at $5/$25 under their own rows. With prefix matching
    gone (#2709), no dated id inherits either price.
    """
    assert estimate_cost(
        "claude-opus-4", input_tokens=1_000_000, output_tokens=0
    ) == pytest.approx(15.0)
    assert estimate_cost(
        "claude-opus-4-8", input_tokens=1_000_000, output_tokens=0
    ) == pytest.approx(5.0)


def test_opus_4_point_releases_priced_at_5_25():
    """Opus 4.5 onward list at $5/M input, $25/M output.

    Anthropic dropped Opus list pricing with 4.5, so each point release
    carries its own $5/$25 entry rather than inheriting the $15/$75 base.
    """
    assert PRICES["claude-opus-4-5"] == (5.0, 25.0)
    assert PRICES["claude-opus-4-6"] == (5.0, 25.0)
    assert PRICES["claude-opus-4-7"] == (5.0, 25.0)
    assert PRICES["claude-opus-4-8"] == (5.0, 25.0)


def test_exact_cost_per_mtok_by_family():
    """1M input + 1M output resolves to the exact list $/MTok per family."""
    # (model, expected input+output cost for 1M each)
    cases = {
        "claude-opus-4": 90.0,  # 15 + 75
        "claude-opus-4-8": 30.0,  # 5 + 25
        "claude-sonnet-4": 18.0,  # 3 + 15
        "claude-sonnet-4-6": 18.0,
        "claude-haiku-4-5": 6.0,  # 1 + 5
        "claude-fable-5": 60.0,  # 10 + 50
    }
    for model, expected in cases.items():
        cost = estimate_cost(model, input_tokens=1_000_000, output_tokens=1_000_000)
        assert cost == pytest.approx(expected), model


@pytest.mark.parametrize(
    "dated",
    [
        "claude-opus-4-20250514",
        "claude-opus-4-8-20260115",
        "claude-sonnet-4-6-20260101",
        "claude-haiku-4-5-20251001",
        "claude-fable-5-20260609",
    ],
)
def test_undeclared_dated_ids_have_no_price(dated):
    """A dated id is priced only when its row declares it as an alias (#2709).

    Longest-prefix matching let an unpriced id borrow another model's price
    (#2531: ``claude-opus-5-5`` metered as ``claude-opus-5``).
    """
    assert resolve_pricing_key(dated, PRICES) is None
    with pytest.raises(UnknownModelCostError):
        require_pricing_key(dated)
    with pytest.warns(UserWarning, match="Unknown model"):
        assert estimate_cost(dated, input_tokens=1_000_000, output_tokens=0) == 0.0


def test_haiku_4_5_pricing():
    """claude-haiku-4-5 is priced at $1/M input, $5/M output."""
    assert PRICES["claude-haiku-4-5"] == (1.0, 5.0)


def test_estimate_cost_exact_match():
    """estimate_cost should work with exact model key matches."""
    # claude-sonnet-4: $3/M input, $15/M output
    cost = estimate_cost("claude-sonnet-4", input_tokens=1_000_000, output_tokens=0)
    assert cost == 3.0

    cost = estimate_cost("claude-sonnet-4", input_tokens=0, output_tokens=1_000_000)
    assert cost == 15.0


def test_sonnet_4_6_carries_the_same_list_price_as_sonnet_4():
    """claude-sonnet-4-6 is priced by its own catalog row, at Sonnet 4's list price."""
    cost = estimate_cost(
        "claude-sonnet-4-6", input_tokens=1_000_000, output_tokens=1_000_000
    )
    expected = estimate_cost(
        "claude-sonnet-4", input_tokens=1_000_000, output_tokens=1_000_000
    )
    assert cost == expected


def test_overrides_for_shorter_ids_do_not_reprice_a_longer_id():
    """An override prices its own id only (#2709): neither shorter id below
    reprices claude-sonnet-4-6, which keeps its catalog row's $3 input price."""
    overrides = {
        "claude-sonnet": (1.0, 2.0),
        "claude-sonnet-4": (9.0, 45.0),
    }
    cost = estimate_cost(
        "claude-sonnet-4-6",
        input_tokens=1_000_000,
        output_tokens=0,
        overrides=overrides,
    )
    assert cost == 3.0


def test_estimate_cost_does_not_match_partial_numeric_suffix():
    """'claude-sonnet-45' must not match 'claude-sonnet-4'."""
    with pytest.warns(UserWarning, match="claude-sonnet-45"):
        cost = estimate_cost(
            "claude-sonnet-45", input_tokens=1_000_000, output_tokens=0
        )
    assert cost == 0.0


def test_estimate_cost_unknown_model_returns_zero():
    """Unknown model IDs should return 0.0, not raise an error."""
    with pytest.warns(UserWarning, match="unknown-model"):
        cost = estimate_cost("unknown-model", input_tokens=1000, output_tokens=1000)
    assert cost == 0.0


def test_estimate_cost_unknown_model_emits_warning():
    """estimate_cost should emit a UserWarning for unknown models."""
    with pytest.warns(UserWarning) as warning_list:
        estimate_cost("gpt-999-turbo", input_tokens=500, output_tokens=500)

    assert len(warning_list) == 1
    message = str(warning_list[0].message)
    assert "gpt-999-turbo" in message


def test_estimate_cost_ollama_returns_zero_silently():
    """ollama/* models are always local/free — return $0 without warning."""
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # any UserWarning becomes an error
        cost = estimate_cost(
            "ollama/qwen3-coder:30b", input_tokens=12_000, output_tokens=42
        )
    assert cost == 0.0


def test_estimate_cost_ollama_with_any_tag_silent():
    """The silent-free behavior applies to any ollama/<model[:tag]> form."""
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        for model in (
            "ollama/llama3",
            "ollama/qwen2.5-coder:14b",
            "ollama/some-future-model:99b-q8",
        ):
            assert estimate_cost(model, input_tokens=1000, output_tokens=500) == 0.0


def test_cache_multipliers_match_anthropic_economics():
    """Cache rates are fixed multiples of the base input price."""
    assert CACHE_WRITE_MULTIPLIER == 1.25
    assert CACHE_READ_MULTIPLIER == 0.10


def test_cache_read_priced_at_tenth_of_input():
    """1M cache-read tokens cost 0.1x the input rate, not full input rate."""
    in_rate, _ = PRICES["claude-sonnet-4"]  # (3.0, 15.0)
    cost = estimate_cost(
        "claude-sonnet-4",
        input_tokens=0,
        output_tokens=0,
        cache_read_tokens=1_000_000,
    )
    assert cost == pytest.approx(in_rate * CACHE_READ_MULTIPLIER)


def test_cache_write_priced_at_1_25x_input():
    """1M cache-creation tokens cost 1.25x the input rate."""
    in_rate, _ = PRICES["claude-sonnet-4"]
    cost = estimate_cost(
        "claude-sonnet-4",
        input_tokens=0,
        output_tokens=0,
        cache_creation_tokens=1_000_000,
    )
    assert cost == pytest.approx(in_rate * CACHE_WRITE_MULTIPLIER)


def test_cache_tokens_default_to_zero():
    """Existing callers (no cache args) are unaffected."""
    in_rate, out_rate = PRICES["claude-sonnet-4"]
    cost = estimate_cost("claude-sonnet-4", input_tokens=1_000_000, output_tokens=0)
    assert cost == pytest.approx(in_rate)


def test_cache_heavy_run_far_cheaper_than_folded():
    """A cache-read-heavy run costs far less than pricing the sum at input rate."""
    in_rate, _ = PRICES["claude-sonnet-4"]
    # 100k real input + 900k cache reads.
    split = estimate_cost(
        "claude-sonnet-4",
        input_tokens=100_000,
        output_tokens=0,
        cache_read_tokens=900_000,
    )
    folded = estimate_cost("claude-sonnet-4", input_tokens=1_000_000, output_tokens=0)
    assert split == pytest.approx((100_000 * in_rate + 900_000 * in_rate * 0.1) / 1e6)
    assert split < folded / 3  # cache-read dominated → much cheaper


def test_compute_cost_from_tokens_passes_cache_args():
    """The keyword wrapper forwards cache token counts."""
    direct = estimate_cost(
        "claude-sonnet-4",
        input_tokens=10,
        output_tokens=5,
        cache_creation_tokens=20,
        cache_read_tokens=30,
    )
    wrapped = compute_cost_from_tokens(
        model="claude-sonnet-4",
        input_tokens=10,
        output_tokens=5,
        cache_creation_tokens=20,
        cache_read_tokens=30,
    )
    assert wrapped == pytest.approx(direct)


def test_cache_pricing_respects_overrides():
    """Cache rates derive from the overridden input rate, not the built-in."""
    overrides = {"claude-sonnet-4": (10.0, 50.0)}
    cost = estimate_cost(
        "claude-sonnet-4",
        input_tokens=0,
        output_tokens=0,
        cache_read_tokens=1_000_000,
        overrides=overrides,
    )
    assert cost == pytest.approx(10.0 * CACHE_READ_MULTIPLIER)


def test_cache_math_is_provider_aware():
    """Cache read/write multipliers come from the resolved entry's provider.

    Anthropic caches read at 0.10x; OpenAI reads at 0.50x. estimate_cost must
    key the cache math off the model's provider, not a flat Anthropic fraction.
    """
    # Anthropic model: 0.10x read (matches the module default).
    anthropic_read = estimate_cost(
        "claude-sonnet-4",
        input_tokens=0,
        output_tokens=0,
        cache_read_tokens=1_000_000,
    )
    assert anthropic_read == pytest.approx(3.0 * 0.10)

    # OpenAI model (priced via override): 0.50x read, 1.0x write.
    overrides = {"gpt-4o": (2.5, 10.0)}
    openai_read = estimate_cost(
        "gpt-4o",
        input_tokens=0,
        output_tokens=0,
        cache_read_tokens=1_000_000,
        overrides=overrides,
    )
    assert openai_read == pytest.approx(2.5 * 0.50)
    openai_write = estimate_cost(
        "gpt-4o",
        input_tokens=0,
        output_tokens=0,
        cache_creation_tokens=1_000_000,
        overrides=overrides,
    )
    assert openai_write == pytest.approx(2.5 * 1.0)


def test_estimate_cost_custom_overrides():
    """Custom overrides take precedence over built-in pricing."""
    overrides = {
        "claude-sonnet-4": (10.0, 50.0),  # Override with different prices
    }
    cost = estimate_cost(
        "claude-sonnet-4",
        input_tokens=1_000_000,
        output_tokens=0,
        overrides=overrides,
    )
    assert cost == 10.0


def test_estimate_cost_override_is_exact():
    """An override prices its own id, never an id that extends it."""
    overrides = {
        "my-custom-model": (1.0, 2.0),
    }
    assert (
        estimate_cost(
            "my-custom-model",
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            overrides=overrides,
        )
        == 3.0
    )
    with pytest.warns(UserWarning, match="Unknown model"):
        cost = estimate_cost(
            "my-custom-model-v2",
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            overrides=overrides,
        )
    assert cost == 0.0


def test_estimate_cost_zero_tokens():
    """Zero tokens should return zero cost."""
    cost = estimate_cost("claude-sonnet-4", input_tokens=0, output_tokens=0)
    assert cost == 0.0


def test_estimate_cost_small_token_count():
    """Should handle small token counts with proper per-token math."""
    # claude-sonnet-4: $3/M input, $15/M output
    cost = estimate_cost("claude-sonnet-4", input_tokens=1000, output_tokens=500)
    expected = (1000 * 3.0 / 1_000_000) + (500 * 15.0 / 1_000_000)
    assert cost == pytest.approx(expected)


def test_require_pricing_key_returns_key_for_known_model():
    """A priced model resolves to its own exact key."""
    assert require_pricing_key("claude-sonnet-5") == "claude-sonnet-5"
    assert require_pricing_key("claude-opus-4-8") == "claude-opus-4-8"


def test_require_pricing_key_raises_on_unknown_model():
    """An unpriced, non-free model hard-errors instead of resolving to $0."""
    with pytest.raises(UnknownModelCostError, match="claude-sonnet-5000"):
        require_pricing_key("claude-sonnet-5000")


@pytest.mark.parametrize("model", ["", "gpt-future-unpriced"])
def test_unpriced_codex_model_refuses(model):
    with pytest.raises(UnknownModelCostError, match="No pricing entry"):
        require_pricing_key(model)


def test_require_pricing_key_exempts_free_provider_models():
    """ollama/* models are $0 by design — no pricing entry required."""
    assert require_pricing_key("ollama/qwen3-coder:30b") == "ollama/qwen3-coder:30b"


def test_require_pricing_key_honors_overrides():
    """A config-priced model counts as priced and does not raise."""
    assert (
        require_pricing_key("my-custom-model", {"my-custom-model": (1.0, 2.0)})
        == "my-custom-model"
    )


def test_build_pricing_overrides_returns_none_for_empty():
    """build_pricing_overrides returns None when given an empty dict."""
    assert build_pricing_overrides({}) is None


def test_build_pricing_overrides_filters_invalid_entries():
    """build_pricing_overrides skips entries that don't have exactly 2 values."""
    raw = {"valid-model": [3.0, 15.0], "bad-model": [1.0], "also-bad": [1.0, 2.0, 3.0]}
    result = build_pricing_overrides(raw)
    assert result is not None
    assert "valid-model" in result
    assert "bad-model" not in result
    assert "also-bad" not in result


def test_build_pricing_overrides_returns_typed_tuples():
    """build_pricing_overrides converts list[float] to tuple[float, float]."""
    raw = {"my-model": [5.0, 25.0]}
    result = build_pricing_overrides(raw)
    assert result is not None
    assert result["my-model"] == (5.0, 25.0)
