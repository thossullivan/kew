# Hermes Runner

Hermes is a headless coding agent that runs against [OpenRouter](openrouter.md). kew reaches it through a thin adapter (`kew/runners/hermes.py`) that shells out to the `hermes` CLI once per dispatch. It applies the same standalone clone isolation, budget gate, audit log, and kill switch.

!!! note "OpenRouter only"
    Hermes runs **OpenRouter models only**. kew deliberately does not wire it to Anthropic or any other provider — route those to `claude-code` or `opencode` instead. A route to `hermes` with a non-OpenRouter model **fails closed** at command-build time rather than silently falling back.

## Setup

1. Install the `hermes` CLI via its `install.sh`.
2. Set `OPENROUTER_PROVISIONING_KEY` as [OpenRouter setup](openrouter.md#setup) describes.
   Every direct OpenRouter run requires it, and hermes runs only OpenRouter models.
   Kew issues a bounded `OPENROUTER_API_KEY` for each hermes run and passes it by name.
   Kew records the key's hash and bounds, never its value.
3. Configure your OpenRouter account's **data policy and credits** before dispatching real code — see the [OpenRouter data-policy prerequisites](openrouter.md#data-policy-prerequisites). Dispatched runs send your repository's code as prompt content, and hermes bills against your OpenRouter credit balance, so set the training opt-out (and optionally pin ZDR providers) first.

## Configuration

Select hermes explicitly — it is **never inferred** from the model name (the `openrouter/` prefix stays mapped to [OpenCode](opencode.md), so existing OpenRouter-via-OpenCode routes are unaffected):

```toml
[agent]
runner = "hermes"
model  = "openrouter/deepseek/deepseek-v4-flash"
```

Or route a complexity tier to it:

```toml
[routing]
default = "claude-code"
simple  = { runner = "hermes", model = "openrouter/deepseek/deepseek-v4-flash" }
```

kew model strings are `openrouter/<vendor>/<model>`. The runner strips the `openrouter/` prefix and invokes hermes as `--provider openrouter --model <vendor>/<model>`.

### `[agent.hermes]` options

| Key | Default | Meaning |
|-----|---------|---------|
| `extra_args` | `[]` | Extra flags passed through to the `hermes` invocation. |

## How it works

Unlike Claude Code / OpenCode / pi, hermes emits **no mid-run event stream**. Stdout is the final response text only. On completion it writes a single JSON **usage file** (kew hands it a run-scoped `--usage-file` path under the log directory, never inside the checkout, so it can never land in a PR branch, and cleans it up on every exit path).

That usage file — not the exit code — is authoritative:

- **Success/failure.** hermes exits `0` even on a credential/config failure. kew trusts the usage file: `completed: true` → success, `failed: true` → a permanent failure (or transient on a rate-limit/network marker). A SIGTERM/timeout kill leaves no usage file and is classified transient.
- **Cost.** kew reads `estimated_cost_usd` from the usage file as the runner-reported native cost. This is hermes's own OpenRouter models-API estimate — the same fidelity class as OpenCode's figure, and framed honestly via the `openrouter:via-hermes` endpoint on the audit record. A killed run with no usage file meters `$0` native and is bounded by the outer budget gate and watchdog.

## Resume

Hermes supports session resume on transient-retry: kew captures the `session_id` from the usage file and resumes with `hermes --resume <session_id>` (a fresh usage file per attempt).

## Next

- [OpenRouter models](openrouter.md) — the data-policy settings to configure before dispatching real code.
- [Runners overview](index.md) — routing and cross-runner cost honesty.
