# kew health

```text
Usage: kew health [OPTIONS]

Options:
  --json
  --window INTEGER
```

Attest that the control plane itself is functioning — a monitoring/CI gate.

```bash
kew health                # human table; exit 0 healthy / 1 degraded / 2 can't read db
kew health --json         # machine-readable result for monitors/CI
kew health --window 14    # retrospective window in days (default 7)
```

## Checks

| Check | What it verifies |
| --- | --- |
| `meter_running` | Successful runs on non-free models recorded cost/tokens (catches silent-zero metering; includes models with no pricing entry). |
| `spend_within_limits` | Daily/weekly spend is within the configured `[budget]` limits (warn at 80%; fixed daily/7-day window, independent of `--window`). Attests spend is within bounds, not per-dispatch enforcement. |
| `audit_parses` | Every `audit_events` row in the window parses end-to-end (payload + envelope fields). |
| `runner_reachable` | Every runner a lane resolves to has its CLI on PATH (+ a credential where detectable). "Every lane" includes the `[review]` runner, which is pinned independently of node routing. The credential names come from the runner's own declaration, so the Claude subscription token (`CLAUDE_CODE_OAUTH_TOKEN`) counts alongside an API key. |
| `dispatch_state` | Dispatch `enabled`, or `warn` (visible, non-paging) when halted by `kew stop` / the circuit breaker — or when `kew serve`'s sweep last beat more than 10 minutes ago, since a dead sweep stops outcome sync, orphan reconciliation and the GitHub auth probe with it. No beat at all stays `pass`: a control plane that has never run `kew serve` is not unhealthy for it. |
| `github_api` | The GitHub credential and remaining API budget, read from the reading `kew serve`'s sweep already cached in the run store — never a GitHub call of its own. `fail` on a `401` (that reading is kew's auth probe), `warn` when the last reading could not be taken, `skip` where no sweep has ever run. A reading older than 10 minutes is `warn` whatever it said: the sweep is its only writer, so once `kew serve` stops that verdict is history, not health. |
| `switchyard` | Skipped in direct mode. In Switchyard mode, bounded `/health` and `/v1/models` probes must admit the configured route. Managed launch separately verifies the pinned binary version and dry run before dispatch. Refusals expose only a sanitized endpoint origin and reason. |

## Switchyard diagnostics

When Switchyard mode is configured, `kew health` also takes one bounded snapshot
from the proxy's `/metrics` endpoint. `--json` includes it under the
`switchyard` check's `detail.proxy_observation` field:

```json
{
  "kind": "switchyard_proxy_observation",
  "scope": "process_wide_proxy",
  "accounting_authority": "runner_reported_usage",
  "per_run_attribution": false,
  "samples": []
}
```

Only documented request, error, latency, routing-overhead, selected model/tier,
and token families are included. Metric labels are allowlisted; response bodies,
credentials, arbitrary labels, and raw transport errors are never printed. If
`/metrics` is unavailable, the same object has an empty `samples` list and a
sanitized `unavailable_reason`. Metrics availability is observational and does
not override the `/health` and route-admission verdict.

These counters describe the whole Switchyard process. They cannot be assigned
to a Kew run when requests overlap, and they never replace runner-reported token
usage or Kew's governed cost calculation.

## Exit codes

See the shared [exit-code scheme](exit-codes.md).

`0` — all checks pass (warnings allowed). `1` — at least one check failed. `2` — the run store could not be read.

`kew health --json` emits `{healthy, exit_code, window_days, checks: [{name, status, summary, detail}]}`.
Direct mode retains the existing schema and does not contact or render
Switchyard diagnostics.
