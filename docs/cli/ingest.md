# kew ingest

```text
Usage: kew ingest COMMAND [ARGS]...
```

```text
Usage: kew ingest file PATH

Arguments:
  PATH
```

```text
Usage: kew ingest serve
```

Ingest Claude Code OTel usage into kew's unified run store. Produces a self-hosted record that spans **dispatched** usage (kew's own runs) and **interactive** usage (a developer running Claude Code by hand), visible in `kew report` as a separate "Interactive (Claude Code)" breakdown.

This is **passive observation, honestly framed**: kew records what Claude Code reports and can alert against a configured cap; it does **not** enforce limits or interrupt an interactive session. Ingested telemetry is **observed, not attested** — it is stored off the tamper-evident hash chain and is not covered by `kew audit verify`.

v1 supports **Claude Code only** (the only runner with a documented OTel export). Other runners are an extension point.

## Surfaces

### `kew ingest file <PATH>`

Batch-ingest a Claude Code OTLP/JSON metrics export file into the run store. Useful for offline ingestion, CI, cron jobs, or any OTel collector that writes to disk.

```bash
kew ingest file /tmp/cc-metrics.json
kew ingest file ./exports/session-2026-06-26.json
```

Output reports how many rows were ingested and how many were skipped (e.g. unknown metrics, cumulative-temporality data points).

### `kew ingest serve`

Start a loopback-only OTLP/HTTP receiver that Claude Code posts to directly — no external OTel collector required. Binds on `127.0.0.1:4318` (default) and accepts OTLP/JSON POST requests at `/v1/metrics`. Press Ctrl-C to stop.

```bash
kew ingest serve
```

Requires `[ingest].enabled = true` in `kew.toml` (disabled by default). The bind host and port are set via `kew.toml` — there are no CLI flags for them. For example, to use a non-default port:

```toml
[ingest]
enabled = true
port = 4319
```

The bind host is loopback-only; see [Configuration](#configuration) below.

## Connecting Claude Code

Set these four environment variables before starting Claude Code to have it post its usage metrics to `kew ingest serve`:

```bash
export CLAUDE_CODE_ENABLE_TELEMETRY=1
export OTEL_METRICS_EXPORTER=otlp
export OTEL_EXPORTER_OTLP_PROTOCOL=http/json
export OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318
```

Claude Code exports metrics on a **delta** temporality by default (each export carries the increment since the previous one). Leave the default in place — the cumulative temporality override (`OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE=cumulative`) is not supported in v1 and any cumulative data points are skipped with a warning.

A minimal shell setup:

```bash
# In your shell profile or session wrapper:
export CLAUDE_CODE_ENABLE_TELEMETRY=1
export OTEL_METRICS_EXPORTER=otlp
export OTEL_EXPORTER_OTLP_PROTOCOL=http/json
export OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318

# Run the receiver in one terminal:
kew ingest serve

# Run Claude Code in another:
claude "fix the failing tests in lib/"
```

## Configuration

The `[ingest]` section in `kew.toml` controls both surfaces:

```toml
[ingest]
enabled = false                 # master switch for `kew ingest serve`
host = "127.0.0.1"              # receiver bind host; loopback-only
port = 4318                     # OTLP/HTTP default
cap_usd = 0.0                   # 0 = no cap alert
combine_with_dispatch = false   # cap against interactive only, or interactive + dispatched
```

See the full [`[ingest]` reference](../configuration.md#ingest) in the configuration guide.

## How it appears in `kew report`

When interactive usage rows exist for the report window, `kew report` adds an "Interactive (Claude Code)" section. Cost figures come verbatim from Claude Code's own `cost.usage` metric — kew does not re-price them from tokens.

```text
## Interactive (Claude Code)

| Model | Tokens | Cost |
|---|---|---|
| claude-opus-4-8 | 840,000 | $3.05 |
| claude-sonnet-4-6 | 210,000 | $0.31 |

Interactive total: $3.36 · Dispatched total: $4.10 · Combined: $7.46
```

The combined line appears only when both channels have data. If there is no interactive data for the window, `kew report` output is byte-identical to today's.

Dispatched cost is kew-priced (tokens × kew's pricing table); interactive cost is CC-reported (Claude Code's own figure). The two numbers are labelled distinctly and never silently merged under one pricing claim.

## Cap alerting

When `[ingest].cap_usd > 0` and interactive spend (or interactive + dispatched when `combine_with_dispatch = true`) meets or exceeds the cap, `kew report` emits one notification via the configured webhook. This is a **reminder alert, not an enforced limit** — kew cannot interrupt an interactive session.

Alerting is stateless: the alert fires once per `kew report` invocation where spend ≥ cap. Cap alerts effectively require `[alerting].webhook_url` — a GitHub notifier has no issue/PR target for interactive usage.

## Exit codes

See the shared [exit-code scheme](exit-codes.md).

| Code | Meaning |
|---|---|
| `0` | File ingested / receiver exited cleanly (Ctrl-C). |
| `2` | File missing, unreadable, or not valid JSON; ingestion failed; or `kew ingest serve` is disabled in config (`[ingest].enabled = false`). |

## See also

[Configuration — `[ingest]`](../configuration.md#ingest) — full field reference and the loopback-only constraint.

[Linear webhooks](../linear-webhooks.md) — receiving Linear issue/comment/project events via `POST /webhooks/linear`, secret setup, and the dev-tunnel recipe.

[Audit log & tamper-evidence](../audit.md) — why interactive usage is stored off the hash chain.

[`kew report`](report.md) — where interactive usage appears in the report.
