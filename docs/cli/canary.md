# kew canary

Attest that kew still reads **fresh** vendor CLI output correctly — the runtime
twin of [`kew health`](health.md). A vendor CLI update can rename a JSON key and
silently break kew's capture of tokens/session/cost; `kew canary` runs the real
runner against a trivial prompt and checks the governance fields the meter
depends on.

```text
Usage: kew canary [OPTIONS]

Options:
  --runner TEXT
  --json
  --record TEXT
  --model TEXT
  --exercise-resume
```

```bash
kew canary                                         # human table; exit 0/1/2
kew canary --runner claude-code --runner opencode  # repeatable (default: both)
kew canary --json                                  # machine-readable JSON (stdout only)
kew canary --record DIR                            # write each runner's raw stream to DIR/<runner>.jsonl
kew canary --runner codex --model gpt-5.6-sol
kew canary --runner codex --model gpt-5.6-sol --exercise-resume
```

## Switchyard passthrough gate

When `inference_transport.mode = "switchyard"`, `kew canary` runs the dedicated
passthrough lifecycle instead of the ordinary direct canary. The configured
route must be exactly `switchyard/passthrough`; random, classifier, and stage
routing are refused. Only runners with a proven transport capability are
eligible (currently Claude Code and Codex; OpenCode remains fail-closed).

This is a live provider exercise and must only run after the plan's explicit
human gate. It also requires an existing owner-only record directory:

```bash
install -d -m 700 /private/path/switchyard-canary
kew canary --runner codex --record /private/path/switchyard-canary --json
```

Kew refuses a missing, symlinked, non-owned, or group/world-accessible record
directory before probing or launching the runner. Raw streams are written as
mode `0600` files named `<runner>-passthrough.jsonl`; raw content is never
placed in normal output, logs, proxy observations, or audit events. Unit tests
use checked-in runner and metrics fixtures and never contact a provider or
proxy.

Before execution, record a retention deadline in the human-approved UAT report.
After reducing the raw evidence to sanitized checks, delete the private record
directory by that deadline and confirm deletion in the UAT report. Kew does not
delete `--record DIR` automatically because it is the operator's review asset.
The passthrough lifecycle always exercises interruption and resume, so
`--exercise-resume` is rejected in Switchyard mode.

## Checks

| Check | Verifies |
| --- | --- |
| `tokens_present` | input and output tokens were captured (> 0). |
| `session_captured` | a session id was captured (runners that emit one). |
| `cost_captured` | opencode's native cost > 0; claude's derived cost (`compute_cost_from_tokens`) > 0. Free models skip. |
| `result_classified` | the run classified `success` with a terminal result event. |
| `tools_exercised` | the resumed passthrough run emitted at least one parsed tool event. |
| `proxy_healthy` | bounded health, route, and metrics checks passed before and after the run. |
| `cancellation_observed` | the interrupted runner process group was terminated and reaped. |
| `resume_completed` | the interrupted session resumed to a terminal result. |
| `cleanup_complete` | runner session, managed proxy/config snapshot, and temporary checkout cleanup completed; the requested raw record remains for review. |
| `session_deleted` | standard session deletion completed with bounded cleanup. |
| `resume_*` | the resumed session was captured, completed, left the tree clean, and passed session deletion. |

## Exit codes

See the shared [exit-code scheme](exit-codes.md).

| Code | Meaning |
| --- | --- |
| `0` | every requested runner ran and all checks passed (full attestation). |
| `1` | a runner ran and a check failed — **drift** (the nightly files a `canary` issue and goes red). |
| `2` | a runner couldn't run (binary missing / unauthenticated / vendor blip) and no drift — **incomplete**, not an alarm. |

## Nightly workflow

`.github/workflows/canary.yml` runs `kew canary` nightly.

- **Self-hosted** (what this repo uses while it is private): claude *and*
  opencode are already installed + authenticated — the only configuration that
  attests **both** runners. No secret needed; ~<1¢/night. The workflow skips
  the canary in a repository that is not private.
- **GitHub-hosted**: installs claude via npm and authenticates it with the
  optional `ANTHROPIC_API_KEY` secret — claude-code only. opencode needs
  provider auth that a clean box lacks, so it reports `skip` (exit 2), not a
  false alarm.

If you fork this repo without a self-hosted runner, drop the `canary` job's
private-repository `if:`, set its `runs-on` to `ubuntu-latest` and add the
secret; the canary then attests claude only.

Refresh the golden fixtures after a deliberate CLI upgrade with
`kew canary --record tests/fixtures/canary` and review the `.jsonl` diff.

Raw `--record` output is sensitive: it can contain prompts, source text,
commands, paths, thread IDs, or secrets. Keep it temporary, replace sensitive
values with type-preserving placeholders, review every JSON line, and delete
the raw directory before committing a sanitized fixture.

Switchyard Prometheus metrics, when captured around a canary, are labeled
`process_wide_proxy` observations. They describe all traffic through that proxy,
not just the canary. Do not subtract concurrent snapshots to assign tokens or
cost to one run. Canary token and cost checks continue to use runner-reported
usage and Kew pricing as their accounting authority.

`--model` is valid only with one requested runner. `--exercise-resume` is valid
only with one resumable runner and writes raw output, when explicitly requested,
to the `--record DIR` directory as `DIR/codex-resume.jsonl`. Codex reachability
requires CLI 0.145.0 or newer and an existing `codex login`; terminal and JSON
output expose only boolean session presence/deletion state, never the session
identifier.

Exit precedence is drift (exit 1), then incomplete (exit 2), then success (exit 0). Both standard
and resumed Codex paths check session deletion; resume additionally requires a
tree-clean result.
