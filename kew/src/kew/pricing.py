"""Cost estimation for dispatch runs, priced from the model catalog (models.json)."""

import hashlib
import warnings
from collections.abc import Mapping
from dataclasses import dataclass, replace

from kew.model_catalog import load_catalog

# Provider prefixes for models that are always free at the API layer (local
# inference, etc.). These return $0 silently — no "unknown model" warning.
_FREE_PROVIDER_PREFIXES: tuple[str, ...] = ("ollama/", "lemonade/")

# Anthropic prices prompt-cache tokens as fixed multiples of the model's base
# input rate, so we derive cache rates rather than maintaining them per model.
# Cache writes are 5-minute ephemeral (the Claude Code default).
CACHE_WRITE_MULTIPLIER = 1.25
CACHE_READ_MULTIPLIER = 0.10


# ---------------------------------------------------------------------------
# Per-provider cache parameter table
#
# Multipliers are time-sensitive — verify against live provider docs when
# updating. Config overrides can supersede these defaults at runtime.
#
# Sources (as of 2026-06):
#   Anthropic: 0.10× read, 1.25× write (5-min ephemeral), explicit caching
#   OpenAI:    0.50× read, 1.0× write  (automatic/implicit, ~5-10 min TTL)
#   Gemini:    0.25× read, 1.0× write  (implicit + explicit, min 32 768 tokens)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProviderCacheParams:
    """Cache economics parameters for a single provider."""

    caches: bool
    read_mult: float
    write_mult: float
    ttl_seconds: int
    min_tokens: int
    style: str  # "explicit" | "implicit" | "implicit+explicit"


PROVIDER_CACHE_DEFAULTS: dict[str, ProviderCacheParams] = {
    "anthropic": ProviderCacheParams(
        caches=True,
        read_mult=0.10,
        write_mult=1.25,
        ttl_seconds=300,
        min_tokens=1024,
        style="explicit",
    ),
    "openai": ProviderCacheParams(
        caches=True,
        read_mult=0.50,
        write_mult=1.0,
        ttl_seconds=600,
        min_tokens=1024,
        style="implicit",
    ),
    "google": ProviderCacheParams(
        caches=True,
        read_mult=0.25,
        write_mult=1.0,
        ttl_seconds=300,
        min_tokens=32768,
        style="implicit+explicit",
    ),
}

_UNKNOWN_PROVIDER = ProviderCacheParams(
    caches=False,
    read_mult=1.0,
    write_mult=1.0,
    ttl_seconds=0,
    min_tokens=0,
    style="none",
)


def get_provider_cache_params(
    provider: str,
    overrides: dict[str, ProviderCacheParams] | None = None,
) -> ProviderCacheParams:
    """Look up cache parameters for *provider*, with optional overrides."""
    if overrides and provider in overrides:
        return overrides[provider]
    return PROVIDER_CACHE_DEFAULTS.get(provider, _UNKNOWN_PROVIDER)


def get_model_cache_params(
    model: str,
    provider: str,
    overrides: dict[str, ProviderCacheParams] | None = None,
) -> ProviderCacheParams:
    """Cache economics for *model*: the provider's, with any catalog multipliers.

    A catalog row (exact id or declared alias) that records its own cache
    read/write multipliers or cache TTL overrides the provider default for
    those fields.
    """
    if overrides and provider in overrides:
        return overrides[provider]
    params = get_provider_cache_params(provider)
    row = load_catalog().lookup(model)
    price = row.price if row is not None else None
    if price is None or (
        price.cache_read_multiplier is None
        and price.cache_write_multiplier is None
        and price.cache_ttl_seconds is None
    ):
        return params
    return replace(
        params,
        caches=True,
        read_mult=_or(price.cache_read_multiplier, params.read_mult),
        write_mult=_or(price.cache_write_multiplier, params.write_mult),
        ttl_seconds=_or(price.cache_ttl_seconds, params.ttl_seconds),
    )


def _or[T: (int, float)](value: T | None, default: T) -> T:
    return default if value is None else value


# ---------------------------------------------------------------------------
# Provider inference from model name
# ---------------------------------------------------------------------------

_MODEL_PROVIDER_PREFIXES: list[tuple[str, str]] = [
    ("claude-", "anthropic"),
    ("gpt-", "openai"),
    ("o1-", "openai"),
    ("o3-", "openai"),
    ("o4-", "openai"),
    ("gemini-", "google"),
    ("gemma-", "google"),
]


def infer_provider(model: str, runner_name: str = "") -> str:
    """Best-effort provider from model ID or runner name. '' if unknown."""
    lower = model.lower()
    for prefix, provider in _MODEL_PROVIDER_PREFIXES:
        if lower.startswith(prefix):
            return provider
    runner_lower = runner_name.lower()
    if "claude" in runner_lower or "anthropic" in runner_lower:
        return "anthropic"
    if "openai" in runner_lower or "gpt" in runner_lower:
        return "openai"
    if "gemini" in runner_lower or "google" in runner_lower:
        return "google"
    return ""


# ---------------------------------------------------------------------------
# prefix_key: stable-input hash for cache-warmth tracking
# ---------------------------------------------------------------------------

PROMPT_TEMPLATE_VERSION = "1"


def compute_prefix_key(
    *,
    repo: str,
    runner: str,
    model: str,
    provider: str,
    repo_map_hash: str = "",
    template_version: str = PROMPT_TEMPLATE_VERSION,
) -> str:
    """SHA-256 of the stable inputs kew controls for prompt-prefix identity.

    Two runs with the same prefix_key on the same provider within the
    provider's cache TTL → the second one's prompt prefix is warm.
    """
    parts = "|".join([repo, runner, model, provider, template_version, repo_map_hash])
    return hashlib.sha256(parts.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Route cost estimation (cold vs warm)
# ---------------------------------------------------------------------------


def estimate_route_cost(
    *,
    model: str,
    provider: str,
    warm: bool,
    est_prompt_tokens: int,
    est_output_tokens: int = 0,
    overrides: dict[str, tuple[float, float]] | None = None,
    cache_overrides: dict[str, ProviderCacheParams] | None = None,
) -> float:
    """Estimate cost for a dispatch route, accounting for cache warmth.

    Cold = full prefill at base rate + write premium on prefix tokens.
    Warm = prefix tokens at read discount + new tokens at base rate.
    Unknown/non-caching providers are always priced as cold with no premium.
    """
    if is_free_model(model):
        return 0.0

    pricing = effective_pricing(overrides)
    best_key = resolve_pricing_key(model, pricing)
    if best_key is None:
        return 0.0

    input_cost, output_cost = pricing[best_key]
    params = get_model_cache_params(model, provider, cache_overrides)

    output_usd = est_output_tokens * output_cost / 1_000_000

    cache_eligible = params.caches and est_prompt_tokens >= params.min_tokens
    if not cache_eligible:
        return est_prompt_tokens * input_cost / 1_000_000 + output_usd

    if warm:
        input_usd = est_prompt_tokens * input_cost * params.read_mult / 1_000_000
    else:
        input_usd = est_prompt_tokens * input_cost * params.write_mult / 1_000_000

    return input_usd + output_usd


Pricing = Mapping[str, tuple[float, float]]


def catalog_pricing() -> dict[str, tuple[float, float]]:
    """List prices per 1M tokens, ``(input_usd, output_usd)``, from the catalog.

    Keyed by every priced row's id and each of its declared aliases, so a
    lookup is exact: adding a model or an alias is a ``models.json`` change.
    """
    table: dict[str, tuple[float, float]] = {}
    for row in load_catalog().rows:
        if row.price is None:
            continue
        rate = (row.price.input_per_mtok, row.price.output_per_mtok)
        for key in (row.id, *row.aliases):
            table[key] = rate
    return table


def effective_pricing(
    overrides: Pricing | None = None,
) -> dict[str, tuple[float, float]]:
    """Catalog prices with config ``[pricing]`` overrides (and the OpenRouter
    overlay, merged into them by the loader) laid on top: an override wins."""
    return {**catalog_pricing(), **(overrides or {})}


class UnknownModelCostError(ValueError):
    """A non-free model has no pricing entry, so its cost would silently be $0.

    Raised by ``require_pricing_key`` on the governed-dispatch path, where a
    silent $0 cost corrupts budgets, the breaker baseline, and the
    cost-per-merged-PR headline. Non-governed telemetry paths keep the lenient
    warn-and-return-0 behavior of ``estimate_cost``.
    """


def require_pricing_key(model: str, pricing: Pricing | None = None) -> str:
    """Resolve *model* to a pricing key or raise ``UnknownModelCostError``.

    Free-provider models (``ollama/*``) are exempt — they are $0 by design, not
    by omission — and return the model ID unchanged. Use this on the governed
    dispatch path so an unpriced routable model fails closed instead of running
    "for free". ``pricing`` defaults to the catalog's prices.
    """
    if is_free_model(model):
        return model
    table = catalog_pricing() if pricing is None else pricing
    key = resolve_pricing_key(model, table)
    if key is None:
        raise UnknownModelCostError(
            f"No pricing entry for model {model!r}: a governed dispatch would "
            f"meter it at $0. Add its row to kew/src/kew/models.json "
            f"or supply a [pricing] override."
        )
    return key


def build_pricing_overrides(
    overrides: dict[str, list[float]],
) -> dict[str, tuple[float, float]] | None:
    """Convert raw config pricing overrides to the typed form used by estimate_cost.

    Filters out entries that don't have exactly 2 values (input, output).
    Returns None if the result is empty (so callers can pass None to estimate_cost).
    """
    result = {k: (v[0], v[1]) for k, v in overrides.items() if len(v) == 2}
    return result or None


def compute_cost_from_tokens(
    *,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_creation_tokens: int = 0,
    cache_read_tokens: int = 0,
    overrides: dict[str, tuple[float, float]] | None = None,
) -> float:
    """Compute cost in USD given a model and accumulated token counts.

    Thin keyword-argument wrapper around ``estimate_cost`` for use in contexts
    that prefer explicit keyword-only calls (e.g. the orchestrator event writer).
    """
    return estimate_cost(
        model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_tokens=cache_creation_tokens,
        cache_read_tokens=cache_read_tokens,
        overrides=overrides,
    )


def is_priced_model(
    model: str, overrides: dict[str, tuple[float, float]] | None = None
) -> bool:
    """True when *model* has a usable rate — a pricing-table/override entry, or
    a free-provider prefix ($0 by design). The non-raising twin of
    ``require_pricing_key``, for callers that want to skip a bound they cannot
    compute rather than warn about a $0 one (#1092)."""
    if is_free_model(model):
        return True
    return resolve_pricing_key(model, effective_pricing(overrides)) is not None


def is_free_model(model: str) -> bool:
    """True for models under always-free provider prefixes (local inference)."""
    return model.startswith(_FREE_PROVIDER_PREFIXES)


def resolve_pricing_key(model: str, pricing: Pricing) -> str | None:
    """Return *model* when the table prices it exactly, else None.

    The catalog table carries each row's id and declared aliases. There is no
    prefix matching: ``claude-opus-5-9`` does not borrow ``claude-opus-5``'s
    price; an id with no row, alias or override has no price (#2709).
    """
    return model if model in pricing else None


def estimate_cost(
    model: str,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_creation_tokens: int = 0,
    cache_read_tokens: int = 0,
    overrides: dict[str, tuple[float, float]] | None = None,
) -> float:
    """Estimate cost in USD for a model invocation.

    The model must be a catalog id or alias, or an override key, exactly.
    Custom overrides take precedence over built-in pricing. Cache tokens are
    priced as multiples of the (possibly overridden) input rate, using the
    resolved model's provider economics — Anthropic reads at 0.10x / writes at
    1.25x, OpenAI reads at 0.50x, etc. (see ``PROVIDER_CACHE_DEFAULTS``).
    Unknown models return 0.0 (and warn). Models under known-free provider
    prefixes (e.g. ``ollama/``) return 0.0 silently.
    """
    if is_free_model(model):
        return 0.0

    pricing = effective_pricing(overrides)
    best_key = resolve_pricing_key(model, pricing)

    if best_key is None:
        warnings.warn(
            f"Unknown model {model!r}: cost estimate will be $0.00",
            UserWarning,
            stacklevel=2,
        )
        return 0.0

    input_cost, output_cost = pricing[best_key]
    cache = get_model_cache_params(model, infer_provider(model))
    return (
        input_tokens * input_cost
        + output_tokens * output_cost
        + cache_creation_tokens * input_cost * cache.write_mult
        + cache_read_tokens * input_cost * cache.read_mult
    ) / 1_000_000
