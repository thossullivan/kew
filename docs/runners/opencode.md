# OpenCode Runner

[OpenCode](https://opencode.ai) is a multi-provider terminal coding agent.
Kew's `OpenCodeRunner` shells out to the OpenCode CLI, giving you a single
integration that covers Gemini, GPT/o-series (including Codex-branded models),
Ollama, OpenRouter, and 70+ other providers. This is distinct from kew's
official `codex` runner, which invokes OpenAI's Codex CLI directly.

## Prerequisites

1. Install the OpenCode CLI — see https://opencode.ai/docs/.
2. Wire up the providers you want to dispatch to. The path differs for cloud vs. local:

   **Cloud providers (Google, OpenAI, OpenRouter, Anthropic):** use `opencode auth login` — credentials are stored in OpenCode's own config.

   ```bash
   opencode auth login google
   opencode auth login openai
   ```

   **Local Ollama:** `opencode auth login ollama` does *not* work — Ollama doesn't fit OpenCode's API-key auth flow. Instead, use Ollama's built-in launcher (Ollama 0.15+) to register your local models with OpenCode in one step:

   ```bash
   ollama launch opencode
   ```

   This walks you through model selection and updates OpenCode's known-models cache. Recommended: pull a model with at least a 64k context window (Qwen3-Coder, Qwen2.5-Coder, etc.) before running it, since OpenCode's agentic loop can chew through context fast.

   Either way, kew does not see or manage provider credentials.

## Selecting a model

Use the `provider_id/model_id` form anywhere kew accepts a model:

```toml
[agent]
model = "google/gemini-2.5-pro"
```

Kew routes any `provider/model` string (and the prefixes `gemini-`, `gpt-`, `o1-`, `o3-`, `o4-`, `ollama/`) to the OpenCode runner. To force OpenCode regardless of the model string, set `runner = "opencode"`.

## Examples

Gemini 2.5 Pro:

```toml
[agent]
model = "google/gemini-2.5-pro"
```

OpenAI Codex:

```toml
[agent]
model = "openai/gpt-5.1-codex"
```

Local Ollama:

```toml
[agent]
model = "ollama/llama3"
```

OpenRouter (multi-segment paths supported):

```toml
[agent]
model = "openrouter/anthropic/claude-sonnet-4-6"
```

## OpenRouter

[OpenRouter](https://openrouter.ai) puts hundreds of models from many vendors behind a single API key, and OpenCode treats it as one more provider. Use the three-segment form `openrouter/<vendor>/<model>`:

```toml
[agent]
model = "openrouter/qwen/qwen3-coder-next"
```

Authenticate once with `opencode auth login openrouter`, or set `OPENROUTER_API_KEY` in the environment.

The contained OpenCode image carries a small provider-model overlay for
OpenRouter ids released after its pinned CLI catalog. The entrypoint copies
that overlay into the fresh per-run OpenCode config and runs `opencode models`
before launch; a missing id is refused as an explicit catalog gap instead of
burning a dispatch on OpenCode's generic server error. Add future post-pin ids
to `docker/opencode.json` when rebuilding the image—there is no need to bump
the OpenCode CLI for a catalog-only change.

OpenRouter models work in `[routing]` tiers like any other model string:

```toml
[routing]
default = "claude-code"
simple = { runner = "opencode", model = "openrouter/qwen/qwen3-coder-next" }
```

Before dispatching real code through OpenRouter, read [OpenRouter models](openrouter.md) — the underlying providers have varying data retention and training policies, and there are account-level settings worth configuring first.

## Resume support

OpenCode supports session resume via `--session <id>`. When kew detects a transient failure (rate limit, timeout, API error), it re-dispatches with the saved session ID and a "continue where you left off" prompt — same behavior as the Claude Code runner.

## Configuration

See [`[agent.opencode]`](../configuration.md#agentopencode) for the full config reference.

## Limitations (v1)

- **Switchyard transport is refused.** The pinned OpenCode contract does not
  yet prove that kew can install an ephemeral, run-owned provider configuration
  with deterministic selection and cleanup and without mutating user or project
  configuration. Selecting `inference_transport.mode = "switchyard"` with the
  OpenCode runner therefore fails before launch with a specific unsupported
  capability error. Direct OpenCode dispatch is unchanged.
- **No native budget gate.** OpenCode does not expose a `--max-budget-usd` flag. Use kew's dispatch-level budget settings instead.
- **No attached-server mode.** Every dispatch starts a fresh `opencode run` process. If cold-boot latency becomes a problem, attaching to a long-running `opencode serve` is a planned future enhancement.
- **Provider auth surfaces as permanent failure.** A missing credential causes OpenCode to exit non-zero with a descriptive error; kew classifies this as permanent and the OpenCode error message appears in the run history.
