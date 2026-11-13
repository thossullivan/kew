# Authentication

kew dispatches Claude Code autonomously — it runs without a human in the loop. This has authentication requirements that differ from interactive Claude Code use.

## Official Codex CLI login

The official Codex runner authenticates on the host through the CLI's own login
cache or operating-system keychain. Run `codex login`, then verify with
`codex login status`. kew checks that official CLI login but never reads,
copies, mounts, prints, or records its credential. `HOME` remains available;
`OPENAI_API_KEY`, `CODEX_ACCESS_TOKEN`, unrelated provider credentials, GitHub
tokens, and AWS credentials are removed. There is no token-env fallback.

## API key required

Set `ANTHROPIC_API_KEY` in your environment before running kew:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

For persistent setup, add it to your shell profile (`~/.zshrc`, `~/.bashrc`) or a `.env` file that you source before running kew.

## Do not use Pro/Max subscription OAuth

> **Warning:** Do not use Claude Pro or Max subscription OAuth tokens for autonomous dispatch. Using subscription OAuth for automated/programmatic access may violate Anthropic's Terms of Service, which restrict subscription accounts to interactive, human-supervised use.

Autonomous dispatch — where kew runs Claude Code in a loop without a human in the loop — requires an API account with an `ANTHROPIC_API_KEY`, not a subscription OAuth session.

See [Anthropic's usage policies and legal page](https://www.anthropic.com/legal) for the authoritative terms.

## Economic rationale

API pricing is consumption-based: you pay per token, only for what you use. This is the correct model for autonomous workloads because:

- **Costs scale with actual work done**, not a flat subscription rate
- **No risk of hitting subscription rate limits** that could silently stall your queue
- **Auditable spend** — every dispatch run logs token usage and cost, and you can set per-run budget caps with `agent.max_budget_usd` in `kew.toml`

For most repositories, a single dispatch run costs $0.50–$5 depending on issue complexity. Use `kew run --dry-run` to preview without spending anything.

## Which credential a *sandboxed* dispatch needs

A contained dispatch authenticates from the **host environment only** — `~/.claude` is deliberately not mounted, so CLI login state can never reach a container. Which env var kew requires (and forwards) is keyed by the **resolved runner**, not by "whatever credential happens to be set":

| Runner | Required in the environment | How to get one |
|---|---|---|
| `claude-code` | `ANTHROPIC_API_KEY` **or** `CLAUDE_CODE_OAUTH_TOKEN` | An API key from [console.anthropic.com](https://console.anthropic.com); `claude setup-token` mints the OAuth token — but see the subscription warning above before using it for autonomous dispatch |
| `opencode`, `pi`, `hermes` | Host `OPENROUTER_PROVISIONING_KEY`; kew issues a bounded `OPENROUTER_API_KEY` per run ([bounded keys](cli/sandbox.md#bounded-openrouter-credentials)) | A management key from [openrouter.ai/settings/management-keys](https://openrouter.ai/settings/management-keys) |
| `codex` | *(none; official CLI login is host-only)* | External container execution fails closed |
| `lemonade` | *(none — its endpoint is host-local, so it cannot be contained)* | — |

Only the resolved runner's own names are forwarded into the container (see [security.md](security.md#the-credential-env-allowlist-is-per-runner)) — a contained opencode run never receives your Anthropic credentials, and a contained claude run never receives your OpenRouter key. Values travel **by name**: kew passes `-e ANTHROPIC_API_KEY`, never the secret itself, so no credential value lands in an argv, a log, the run history, or the audit ledger.

If the resolved runner's credential is missing, kew **refuses the dispatch before launching the container** and tells you which var to export — a container that comes up "Not logged in" burns a dispatch for nothing. You can check the whole path ahead of time with:

```bash
kew sandbox smoke --runner opencode
```


### OpenRouter per-run issuance

Set `OPENROUTER_PROVISIONING_KEY` on the host to enable bounded keys for direct OpenRouter runs.
Kew forwards only the issued `OPENROUTER_API_KEY` by name for that launch.
Kew never forwards, logs, or stores the provisioning value.
The key uses a finite no-reset dollar limit and a UTC expiry.
Foreground runs revoke it at run end and record its identity and bounds only.
The epic driver mints detached keys in the parent and revokes them on subsequent ticks.
Children never receive provisioning authority.
Expiry bounds revocation failures when no driver returns.
See [sandbox credentials](cli/sandbox.md#bounded-openrouter-credentials) for the exact budget and timeout rules.
Raw ambient `env:` credentials publish only a head the host scanned clean immediately before the push.
Uncontained/native host runs routed directly through OpenRouter are refused without provisioning, like contained runs; the ambient `OPENROUTER_API_KEY` never enters a child process.
Their host boundary is the operator's own machine.
Contained direct OpenRouter runs require host `OPENROUTER_PROVISIONING_KEY` before launch.
Missing provisioning causes `OpenRouter provisioning key required for direct routes` and exit 2.
See [OpenRouter setup](runners/openrouter.md#setup) to configure the provisioning key.
Direct OpenRouter epic and delegation runs require a parent provisioning key.
Native Claude, native Codex, and Switchyard routes require no bounded key or issued-key marker.
The standalone delegation watcher reconciles keys each pass and on exit.
Generic helpers never inherit an issued bounded key or its marker.
The sandbox smoke command still requires an existing inference key.
