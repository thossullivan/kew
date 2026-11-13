---
description: The coding agents kew can run, what each one is for, and how each one is contained.
---

# Runners

kew is **runner-agnostic**. It owns the queue, standalone clone isolation, budgets, security hook, tamper-evident audit log, and governance. It treats the coding agent as a pluggable, supervised subprocess. The pre-dispatch budget caps, the audit log and the `kew stop` latch apply to every runner; the caps count only the spend a runner reports. Approval gates apply when `[approval] threshold_usd` is set. The in-run cost watchdog can stop a run only on a runner that reports cost: the [capability manifest](#runner-capability-manifest) lists each runner's cost signal. The security hook is a Claude Code hook. External-containment runners use containers. Official Codex uses its own sandbox. Unavailable containment routes fail closed. Host-execution paths listed in [Security](../security.md#sandboxed-dispatch-credential-posture) have no kew guard.

> Governance that lives inside one vendor's console governs one vendor. kew keeps budgets, audit, approval, and the kill switch **outside** the runner, cloud or local. What each control can measure depends on what the runner reports.

## Supported runners

| Runner | Use it for | Models | Config section |
|--------|-----------|--------|----------------|
| **`claude-code`** *(default)* | Deep Claude integration with session resume and structured events | Claude | `[agent.claude_code]` |
| **`opencode`** | Multi-provider and local models; GPT/o-series inference remains here | Gemini, GPT/o-series, Codex-branded models, Ollama, OpenRouter | `[agent.opencode]` |
| **`codex`** | Official OpenAI Codex CLI, explicitly selected and host-brokered; supports Switchyard through OpenAI Responses *(experimental)* | Authenticated Codex models | `[agent.codex]` |
| **`pi`** | The [pi](https://pi.dev) coding agent | Multi-provider | `[agent.pi]` |
| **`hermes`** | Headless OpenRouter-only agent with a runner-produced usage-file cost estimate | OpenRouter | `[agent.hermes]` |
| **`lemonade`** *(experimental)* | kew's local loop over an OpenAI-compatible endpoint | Local models | `[agent.lemonade]` |

## Runner capability manifest

This manifest records implemented behavior from `RUNNER_CAPABILITIES`.
These are adapter declarations, not current route readiness.

| Runner | Resume | Telemetry | Cost signal | Loop behavior | Containment requirement |
| --- | --- | --- | --- | --- | --- |
| `claude-code` | `session` | `activity`, `tool_use`, `tokens`, `terminal` | `native_total` | `native` | `external` |
| `codex` | `session` | `activity`, `tool_use`, `tokens`, `terminal` | `unavailable` | `native` | `native` |
| `opencode` | `session` | `activity`, `tool_use`, `tokens`, `terminal` | `native_incremental` | `native` | `external` |
| `pi` | `session` | `activity`, `tool_use`, `tokens`, `terminal` | `native_incremental` | `native` | `external` |
| `hermes` | `session` | `activity`, `tokens`, `terminal` | `estimated_total` | `native` | `external` |
| `lemonade` | `session` | `activity`, `tool_use`, `tokens`, `terminal` | `unpriced` | `native` | `unavailable` |

`session` resume means the adapter retains a session identifier.
Lemonade retains its session identifier but does not replay prior transcript state.

These declarations exclude verification, review, publication, metering, tracker workflow, and exclusion evidence.
Those controls belong to separate workflow owners.
Native loop behavior does not certify every first-class Kew loop path.
Native containment does not imply container execution.
Official Codex stays on the host under its native sandbox on the `docker` and `podman` runtimes, and is contained by kew on `openshell` ([details](codex.md#posture-per-runtime)).
Hermes reads a runner-produced cost estimate from its usage file.
Lemonade cannot use the existing contained route.
Unpriced local inference does not mean zero operating cost.
An unavailable native dollar total does not mean absent token accounting.

## Runner security capabilities

This table is generated from each runner's security capability declarations.

| Runner | External container | Native containment | Command guard | Credential names | Network support |
| --- | --- | --- | --- | --- | --- |
| `claude-code` | yes | no | yes | `ANTHROPIC_API_KEY`, `CLAUDE_CODE_OAUTH_TOKEN` | yes |
| `codex` | no | yes | no | none | yes |
| `opencode` | yes | no | no | `OPENROUTER_API_KEY` | yes |
| `pi` | yes | no | no | `OPENROUTER_API_KEY` | yes |
| `hermes` | yes | no | no | `OPENROUTER_API_KEY` | yes |
| `lemonade` | no | no | no | none | no |

External network support means the container path accepts Kew's network boundary.
Native network support means the runner enforces its own boundary.
Command guard support does not prove that a checkout loaded the guard.
The run posture records `loaded` only when the target matches the trusted hook.

## Runner comparison

The following table compares the available runners to help you choose the right one for your needs:

| Runner | Provider(s) | Cost | Best for |
|--------|-------------|------|----------|
| **Claude Code** | Anthropic | Metered | Hard work |
| **OpenCode** | Multi-provider | Metered | Provider-flexible |
| **Official Codex** | OpenAI direct | API-equivalent included-plan value | Explicit host dispatch |
| **pi** | Multi-provider | Metered | Minimal terminal agent |
| **Hermes** | OpenRouter only | Metered estimate | OpenRouter models, runner-produced usage-file cost estimate |
| **Lemonade** *(experimental)* | Any local OpenAI-compatible endpoint | Unpriced | Supervised local evaluation |

Each runner is a thin adapter (in `kew/runners/`) that kew shells out to and supervises. Per-runner options — binary path, extra flags, provider knobs — live in that runner's `kew.toml` section; see [Configuration](../configuration.md).

## Switchyard transport

!!! warning "Experimental"
    Switchyard transport remains rollout-gated and requires human approval.
    Use it only with supervision and the required pinned configuration.

Claude Code supports the optional Switchyard transport through its native
Anthropic Messages interface. Kew applies the configured endpoint with Claude
Code's documented, child-local `ANTHROPIC_BASE_URL` override and supplies the
exact configured route through `--model`. Fresh and resumed sessions use the
same override, and Kew does not write Claude project or user configuration.

Direct mode remains the default and does not change or remove an
operator-provided Anthropic endpoint. Switchyard owns upstream provider
credentials; endpoint and route configuration contain no credential values.

When the inference transport is `switchyard`, the official Codex runner uses
the configured route as its exact model and supplies a run-local custom
provider over the OpenAI Responses wire API. The endpoint and provider are
passed only through Codex CLI configuration overrides; kew does not write user
or project Codex configuration. Codex receives the endpoint's `/v1` API base;
JSONL events, native resume, reasoning effort, and the existing host
authentication remain unchanged.

Switchyard support is earned per runner; speaking a compatible API is not
enough. The v1 rollout matrix is intentionally narrow:

| Runner | Switchyard v1 status | Boundary |
| --- | --- | --- |
| `codex` | Passthrough-capable | OpenAI Responses; explicit selection and Codex CLI 0.145.0+; native authentication may be presented to the configured endpoint. |
| `claude-code` | Passthrough-capable | Anthropic Messages; child-local `ANTHROPIC_BASE_URL`; native authentication may be presented to the configured endpoint. |
| `opencode`, `pi`, `hermes`, `lemonade` | Refused | No proven isolated v1 override; Kew fails before runner launch. |

Passthrough capability is not rollout approval. Direct remains the default,
and the exact binary, route configuration, runner/model, corpus, canary, and
benchmark evidence require the independent UAT gate
(a maintainer-held record that is
not published). Random,
classifier, stage routing, and automatic direct fallback remain disabled.
Runner-reported tokens/cost stay authoritative; process-wide proxy metrics are
diagnostic observations only. See
[Switchyard operations](../operations.md#switchyard-passthrough-operations)
for startup, failure, shutdown, and Rollback procedures.

## Running a runner in a sandbox

With `[sandbox] enabled = true`, kew selects the runner's declared containment requirement.
External containment runs in a container image.
Native containment can remain on the host.
An unavailable containment route fails closed.
Container execution uses a fixed, normative host interface.
See the [container entrypoint contract](../sandbox-entrypoint-contract.md).
Use that contract when you build a new agent image.
Per-runner variation belongs *inside* the image, not in kew.

!!! warning "Containability is declared per runner"

    A resolved container runner needs both a mapped image and a nonempty
    approved credential declaration. Missing either half fails closed. Official
    Codex declares runner-native containment in code instead, and the
    declaration is read **per runtime**. On `runtime = "docker"` or `"podman"`
    it stays on the host under Codex `workspace-write` and records posture
    `native`; mapping an image is neither required nor used. On
    `runtime = "openshell"` kew holds the stronger boundary, so a gateway-launched
    Codex dispatch is contained and *does* need `[sandbox.images].codex` mapped
    — see [the Codex reference](codex.md#posture-per-runtime) for the runs on
    that runtime which stay `native`. Configuration cannot grant any runner
    native status.

    Do not route a non-Claude runner through the legacy single `[sandbox].image`;
    use an explicit `[sandbox.images]` mapping and the runner's own declared
    credential. No runner borrows the Claude image or credential.

## Routing

Set a `default` runner, then optionally route by issue **complexity** (`simple` / `medium` / `complex`). Each tier can pick its own runner and model:

```toml
[routing]
default = "claude-code"                                                            # default runner
simple  = { runner = "opencode",    model = "openrouter/google/gemini-3.8-flash" } # cheap model for easy issues
complex = { runner = "claude-code", model = "claude-opus-4-8" }                    # strongest for hard issues
```

A tier may be a full `{ runner, model }` table or a bare string shorthand (`medium = "claude-code"`). kew classifies each issue's complexity (from labels / heuristics) and picks the matching tier, falling back to `default`.

### Inferring the runner from the model

If you set only a **model**, kew infers the runner from the model name, so most setups need no explicit `runner`:

| Model prefix | Runner |
|--------------|--------|
| `claude-*` | `claude-code` |
| `gemini-*`, `gpt-*`, `o1-*`, `o3-*`, `o4-*`, `ollama/*` | `opencode` |

`pi`, `hermes`, and `codex` are never inferred. Select them explicitly. The
`openrouter/*` prefix stays mapped to `opencode`, and GPT/o-series inference
continues to select OpenCode rather than the official Codex runner.

## Cost & honesty across runners

kew records runner dollar signals when available.
The signal can be a total, an increment, or a runner-produced estimate.
Some runners report token usage without a native dollar total.
Unpriced local inference still uses hardware and electricity.

Costs are **list-price/API-equivalent**, not necessarily your actual bill.
See the value-leverage framing in [`kew report`](../cli/report.md).
The per-model scorecard compares your fleet's merge rate, review cycles, and cost per merged PR.
Run `kew report --models` to view that scorecard.

For runners that emit OpenTelemetry usage (e.g. Claude Code interactive sessions), `kew ingest` folds that into the same record so dispatched and interactive spend live in one place.

## Next

- [OpenCode runner](opencode.md) — provider auth and setup for Gemini / GPT / Ollama / OpenRouter and 70+ others.
- [Official Codex runner](codex.md) — official login, native sandbox, and host publication.
- [OpenRouter models](openrouter.md) — hundreds of models behind one key via OpenCode, plus the data-policy settings to configure before dispatching real code.
- [Configuration](../configuration.md) — routing and all `[agent.<runner>]` sections.
