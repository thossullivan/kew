# Cache-Aware Routing & TTL-Aware Dispatch Ordering — Design Spec

**Date:** 2026-06-28
**Status:** Approved design, pre-implementation
**Author:** brainstormed with Claude (deep-research + brainstorming skills)

## Summary

Add a **cache-economics layer** to kew that makes its existing model/runner routing and
dispatch ordering aware of provider prompt-cache economics. The feature is delivered in three
phases — **observability → routing governor → TTL-aware ordering** — and ships as a pure
*advise-only* capability by default, with two independent opt-in flags to enable automatic
behavior.

The wedge (validated by two adversarially-verified research passes, see Background): pricing the
**mid-session/cross-session model-switch cache loss** ("cached-input checkout delta") and ordering
dispatch by cache-TTL warmth is **genuinely unoccupied** across the entire LLM-gateway field. kew
is uniquely positioned because it already owns the two prerequisites: the **dispatch queue**
(ordering lever) and the **per-step cost ledger** (the cache-state oracle).

## Background & research basis

Two verified deep-research passes (2026-06-28) established:

- **Mechanism:** provider prompt caching reuses KV tensors for a byte-exact prefix. Cache reads
  are ~0.1× base input on Anthropic (10× cheaper); writes carry a 1.25× (5-min TTL) / 2× (1-hr)
  premium, so caching only pays on reuse. Hits require an identical prefix in order
  tools→system→messages; any change invalidates that level and everything after. Disciplined use
  yields 41–80% cost / 13–31% TTFT gains (arXiv 2601.06007).
- **Hard constraint:** kew calls hosted provider APIs, so it cannot do server-side replica routing
  (vLLM/SGLang/Ray/llm-d/Clarifai are mechanism prior-art only). kew's levers are prompt-prefix
  discipline, dispatch ordering, sticky keys, and model/provider selection.
- **Competitive gap (12 gateways examined):** cache-*economics*-aware cross-provider routing +
  TTL-aware ordering is unoccupied. vLLM SAAR has the "checkout delta" concept but only at the
  self-hosted inference tier. OpenRouter does single-model provider-affinity gated on
  "cache-read < normal price" (good prior art for the engage-gate). LiteLLM has crude prefix-affinity
  (full-prefix hash, hardcoded 300s TTL) and header bugs that *break* caching on Vertex/Bedrock.
  Portkey/Helicone are pass-through + observability only (and their "cache metrics" largely measure
  their own response cache). Kong/Requesty route on nominal cost/semantic with no cache dimension.

The cross-runner corollary: **caches never cross providers**, so any cross-provider switch is a
guaranteed cold start — a re-prefill cost the nominal task-router ignores. This makes the governor
*more* valuable in kew's multi-runner setup, not less.

## Goals & non-goals

**Goals**
- Surface cache read/write/uncached economics from the ledger (observability).
- Make routing account for warm-prefix cost so cost-driven model choices don't backfire.
- Order dispatch so prefix-sharing sessions run back-to-back inside the cache TTL.
- Be measurable: prove $ savings on kew's own dogfood dispatch queue.
- Exercise/stress-test the kew autonomous loop by implementing this feature via the loop itself.

**Non-goals (explicitly deferred to separate future features)**
- A richer task-aware/quality classifier (RouteLLM/IPR-style auto difficulty detection). This
  feature builds only the *seam* it plugs into.
- The "stagger same-prefix across worker slots" ordering refinement.
- A full learned/predictive economic model ("approach B"); the heuristic ledger-backed model is
  designed to let B layer on later without rework.

## Success criteria

Primary: **measured cost reduction on a real dogfood workload** (kew running its own issue queue),
with before/after numbers from the ledger, plus a **calibration** check (did the governor's
predicted savings ≈ actual?). Secondary: the feature doubles as a non-trivial loop stress-test.

## Decisions locked during brainstorming

1. **Scope:** all three sub-capabilities as one phased feature (observability → routing → ordering).
2. **Autonomy:** opt-in auto, default off (advise-only until a flag is set). Mirrors kew's
   cost-cap "alert not enforce" pattern.
3. **Technical spine:** ledger-backed heuristic ("approach A"), designed B-ready.
4. **Task routing:** governor + seam only; the classifier is a separate future feature.
5. **Delivery:** spec → GitHub issues (spec-linked) → implement via the kew loop (dogfood),
   replacing the generic writing-plans step.

## Architecture

A cache-economics layer woven through four existing seams, all backed by the cost ledger. **No new
source of truth — the ledger is the cache-state oracle.**

```
   ready issues → SELECTOR (selector.py)
                  priority+age sort → cache-TTL reorder WITHIN tier   ◄── Phase 3
                      │
                      ▼
   per issue     → ROUTING (registry.resolve_runner)
                  stage 1: propose candidate (label tiers — the SEAM)
                  stage 2: cache-economics GOVERNOR                    ◄── Phase 2
                      │
                      ▼
                  DISPATCH → runner → provider API
                  records cache tokens (exists) + prefix_key + provider + observed_at
                      │
                      ▼
                  LEDGER (run_store) ── warm-prefix index
                  "is prefix P warm on provider X right now?"
                      │
                      ▼
                  REPORT (report.py) cache section + leverage banner   ◄── Phase 1
```

### The one new concept: `prefix_key`

A hash of the stable inputs kew controls: `(repo, runner, model, provider, prompt-template
version, repo-map hash)`. Two runs with the same `prefix_key` + same `provider` within that
provider's TTL → the second one's prefix is **warm**.

**Known approximation (stated honestly):** `prefix_key` does not capture the runner's *internal*
system+tools prompt (e.g. Claude Code builds its own), but those are byte-constant for a given
runner version and the provider caches them automatically; a runner version bump simply shows up as
more cold runs until things re-warm. Sufficient for the heuristic; documented as a limitation.

### Fail-safe invariants (the spec's contract)

1. **Priority dominates** — ordering never crosses priority tiers.
2. **Override-up-only** — the governor may only substitute an equal-or-stronger warm model; it
   never trades quality for cache savings. Net effect: spend ≤, quality ≥, always.
3. **Respects explicit pins** — an explicit per-issue model choice is never governed.
4. **Additive / fail-safe** — flags off or data missing → *exactly* today's behavior.
5. **Every decision is attested** — governor + ordering choices are written to the hash-chained
   audit, replayable.
6. **Anti-churn threshold** — never override for negligible savings.

## Component design

### Phase 1 — Ledger + cost model + observability

**Ledger (`run_store.py`):** cache token columns already exist (v11). Add two additive columns and
bump schema v15 → v16:

| Column | On | Purpose |
|---|---|---|
| `prefix_key` TEXT | `runs` (+ `interactive_usage`) | the stable-input hash above |
| `provider` TEXT | `runs` (+ `interactive_usage`) | derived from runner+model; cache is per-provider |

- Computed and recorded at dispatch time in `dispatch_core.execute_dispatch` (alongside the
  existing cache-token write, ~line 556).
- Back-compat: old rows have `NULL` → treated as cold/unknown. No history migration needed.
- Warm-prefix oracle: `warm_prefixes(provider, now) -> set[prefix_key]` — distinct `prefix_key`
  from runs where `provider=? AND observed_at > now - ttl AND cache tokens > 0`. Pure read.

**Per-provider cache-parameter table** (ships with defaults, **overridable in config**):

| provider | caches? | read mult | write mult | TTL | min tokens | style |
|---|---|---|---|---|---|---|
| anthropic | yes | 0.10× | 1.25× (5m) | 300s | 1024 | explicit |
| openai | yes | ~0.25–0.5× | 1.0× (implicit) | ~5–10m | 1024 | implicit |
| google/gemini | yes | ~0.25× | varies | varies | varies | implicit + explicit |
| z.ai / GLM, unknown | **default false** | — | — | — | — | confirm before enabling |

- Multipliers are **time-sensitive** → live in data with a "verify against live docs" note and
  config override (today's hardcoded 1.25/0.10 generalize to this table, Anthropic values
  unchanged).
- Implicit ≠ explicit: OpenAI/Gemini cache automatically with no write premium (write mult 1.0).
- Unknown providers default `caches=false` (treated as always-cold; conservative).

**Cost model (`pricing.py`):** generalize the hardcoded multipliers, add:

```
estimate_route_cost(route, warm: bool, est_prompt_tokens) -> expected_usd
  cold:  prompt·input_rate + write_premium·prefix_tokens + output…
  warm:  (prefix_tokens·read_mult + new_tokens)·input_rate + output…
```

**Observability (`report.py`):** add a cache section — per (runner, model, provider) cache
read/write/uncached tokens, hit ratio, **$ saved vs all-cold baseline**, and "missed warmth" (cold
runs that had a warm prefix available). Fold "cache saved you $X / Y%" into the existing
cost-semantics leverage banner. Includes **baseline capture** so the dogfood savings number is not
confounded by the recursion of the loop building the feature.

### Phase 2 — Routing governor (`registry.resolve_runner`)

Two-stage pipeline: `propose_candidate()` (today's tier logic, untouched) → `cache_governor(candidate, ctx)`.
The governor is pure: ledger + param table in, decision + rationale out.

Decision steps:
1. **Engage gate** (borrowed from OpenRouter): proceed only if the proposed provider caches and its
   read-mult < 1.0. Otherwise passthrough.
2. **Candidate set:** `{proposed} ∪ {warm models whose capability ≥ proposed}`. Capability order
   comes from the existing tier ladder (simple<medium<complex).
3. **Price each** with `estimate_route_cost` (warm vs cold from the oracle).
4. **Pick minimum expected cost.** If a warm, equal-or-stronger model beats the cold proposal by
   more than `governor_threshold_usd` → recommend it. This comparison *is* the checkout delta.

Guardrails: override-up-only (never weaker); respects explicit pins; no warm prefix → passthrough.

Modes:
- `cache_aware=false` (default): compute + log + write attested record
  `{proposed, would_choose, est_cost_each, warm_set, reason, applied:false}`, **return original
  proposal unchanged**.
- `cache_aware=true`: return overridden route; same record with `applied:true`.

Explainability: every decision reconstructs from ledger + params (no hidden state). A
`--dry-run`/preview surfaces per-issue reasoning ("proposed Haiku (cold, $0.21); Opus warm for this
repo ($0.14) → recommend Opus, save $0.07"). That dry-run is the advise-mode UX.

### Phase 3 — TTL-aware dispatch ordering (`selector.py`)

After the existing priority+age sort, add a **within-tier reorder pass** that clusters issues
sharing a `prefix_key` adjacently so they dispatch back-to-back inside the provider TTL (first warms,
rest read). Secondary within-tier sort key: `(shared-prefix cluster, age)`.

- Bounded: never crosses tiers; `max_reorder_lookahead` cap prevents starving age order.
- **Parallelism nuance (honest):** with `max_workers > 1`, simultaneously-fired same-prefix issues
  all start cold. v1 clusters them adjacently and lets natural worker cadence warm them; the
  "stagger across slots" refinement is deferred.
- Advise vs auto: advise logs the suggested order; auto reorders.

## Config

Extends `RoutingConfig`, adds `CacheConfig` + `ProviderCacheParams` (pydantic, validators: mults ≥
0, ttl > 0). Zero-config = advise-only with correct Anthropic defaults.

```toml
[routing]
cache_aware = false             # routing governor: auto vs advise-only

[routing.cache]
governor_threshold_usd = 0.02   # anti-churn: min savings to override
order_aware = false             # phase-3 ordering: auto vs advise
max_reorder_lookahead = 10

[routing.cache.providers.anthropic]   # the param table, overridable
read_mult = 0.10
write_mult = 1.25
ttl_seconds = 300
min_tokens = 1024
caches = true
# z.ai / unknown providers: caches defaults false until set
```

Two independent switches — `cache_aware` (governor) and `order_aware` (ordering) — both default
**false**, so the whole feature ships as pure advise-only and each is opted into auto separately.

## Testing strategy

**Unit:**
- Cost model: per-provider cold vs warm; implicit providers have no write premium; unknown provider
  (`caches=false`) → always cold; threshold boundaries.
- `prefix_key`: stability (same inputs → same key) and sensitivity (repo-map / template change →
  different key).
- Governor decision table: warm-stronger beats cold-cheaper; **override-up-only enforced** (never a
  weaker warm model); gate skips when read-mult ≥ 1; explicit pin respected; no-warm → passthrough;
  advise vs auto return values.
- Warm-prefix oracle: TTL boundary just-inside/just-outside; provider isolation.
- Ordering: within-tier clustering, priority invariant preserved, lookahead bound, determinism.

**Contract/audit:** governor + ordering decisions are well-formed attested facts; v16 migration
round-trips; old `NULL` rows read as cold.

**Dogfood/integration (the success metric + loop stress-test):**
1. Capture baseline first (advise-only over kew's own queue; record predicted savings without
   applying).
2. Enable auto, run the loop, compare actual ledger cost vs baseline *and* vs predicted (calibration).

Per standing norms: each issue runs the **exact CI gates** on its own work (ruff, xenon on dense
functions, bandit on host/secret-looking literals, tests) and **live-dogfoods the real CLI→git/gh
paths**, not just mocks.

## Issue decomposition (kew-loop work-list)

Each independently mergeable, spec path linked in the body, CI green on its own work.

| # | Phase | Scope | Deps |
|---|---|---|---|
| 1 | Observability | v16 schema (`prefix_key`,`provider`), record-at-dispatch, `warm_prefixes()` oracle, per-provider cost model + param table, `kew report` cache section + leverage-banner line, baseline capture. No behavior change. | — |
| 2 | Governor | two-stage `resolve_runner`, candidate set + override-up-only, checkout-delta compare, `cache_aware` (advise/auto), audit records, `--dry-run` reasoning | #1 |
| 3 | Ordering | within-tier reorder, `order_aware` flag, bounds, advise/auto, audit | #1 |
| 4 | Docs/positioning | `CacheConfig` reference in `docs/configuration.md`, the kew.dev "cache-aware routing" positioning note | #2, #3 |

**Loop sequencing:** #1 first (everything depends on it), then #2 and #3 concurrently, then #4. The
baseline-before-auto rule lives in #1 so the recursion doesn't confound the savings number.

## Risks & open items

- **Per-provider multipliers drift** — mitigated by data-not-code + config override + "verify
  against live docs" note.
- **z.ai/GLM caching support unconfirmed** — mitigated by default `caches=false` (conservative).
- **Parallel cold-start** — accepted for v1 (cluster-adjacent); stagger refinement deferred.
- **Cross-session warmth may be modest** in practice — Phase 1 observability exists precisely to
  measure whether Phases 2–3 are worth turning on; advise-only default means we learn before we act.
- **Recursion/measurement confound** — baseline captured before the loop applies auto.
- **Martian / Unify** competitive coverage unverified (minor positioning gap, not a build risk).
