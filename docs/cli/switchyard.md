# kew switchyard

```text
Usage: kew switchyard COMMAND [ARGS]...
```

```text
Usage: kew switchyard benchmark [OPTIONS]

Options:
  --corpus PATH
  --runner TEXT
  --model TEXT
```

!!! warning "Experimental"
    Switchyard transport remains rollout-gated and requires human approval.
    Use it only with supervision and the required pinned configuration.

Human-gated evidence tools for Kew's optional Switchyard inference transport.
The first command is a fixed-corpus, direct-versus-passthrough benchmark:

```bash
kew switchyard benchmark \
  --corpus /private/path/switchyard-corpus.json \
  --runner codex \
  --model gpt-5.6-sol > benchmark.json
```

This is a live provider exercise. Do not run it until the rollout plan's human
gate has approved the exact corpus, model, credentials, runner version, and
Switchyard build. Unit tests use injected executions and metrics fixtures; they
do not launch a proxy or contact a provider.

## Safety contract

The benchmark refuses to run unless all of these are true:

- `inference_transport.mode` is `"switchyard"`;
- the route is exactly `"switchyard/passthrough"`;
- Switchyard is configured in managed mode with its pinned binary, digest,
  immutable route configuration, and expected version;
- authenticated routes declare the exact reviewed credential variable names
  required by their route clients, with nonempty values supplied only through
  the host environment;
- the runner has a proven passthrough override (`codex` or `claude-code`); and
- one explicit model is supplied for both legs.

Random, classifier, stage, and fallback routing remain disabled. The direct
leg runs first, sequentially, followed by the passthrough leg in the same case
order. The harness does not mutate global runner configuration.

## Corpus format

The corpus is bounded JSON with no unknown fields. Case IDs and quality-check
names must be unique. `expected_proxy_requests` is explicit because a case may
legitimately require more than one inference request.

```json
{
  "schema_version": 1,
  "name": "passthrough-smoke-v1",
  "cases": [
    {
      "id": "exact-answer",
      "prompt": "Reply with exactly BENCHMARK_OK.",
      "quality_checks": [
        {"name": "expected-marker", "contains": "BENCHMARK_OK"}
      ],
      "expected_proxy_requests": 1
    }
  ]
}
```

Kew reads the file once and records the SHA-256 digest of its exact bytes. The
report contains IDs and boolean check results, never prompts or runner response
text. Keep the corpus itself free of credentials and private source.

## Evidence and accounting

Output is canonical JSON: keys are sorted, separators are stable, and no
timestamp, temporary path, session ID, prompt, or raw response is included.
Each case records quality checks, wall latency, and runner-reported input,
output, cache, model, and native-cost fields. The report binds the corpus,
direct and passthrough configuration digests, exact managed route-config
digest, runner version digest, and Switchyard version digest.

Runner output is the accounting authority for task usage. Raw Switchyard
Prometheus values are labeled as process-wide proxy observations and never
substitute for runner-reported task usage or independently establish task
billing. A managed dispatch brackets its fresh, run-owned proxy with baseline
and final snapshots; only the positive counter delta is attributed to that run
for served-model, tier, and classifier-overhead reconciliation.

For a managed adaptive route, Kew may reconcile a bounded selected model and
tier from the structured runner response or unambiguous Switchyard selection
metrics. The run record and audit evidence keep that served identity separate
from the admitted route alias. A classifier route may also show separately
observed classifier input/output tokens and cost; those values are never
silently presented as task usage. The normal report exposes the route, served
model, tiers, and classifier overhead in its Switchyard accounting table.
The supervisor takes its final snapshot before termination and waits for its
stderr drainers after termination. Kew then attaches the completed classifier
records and selection evidence before computing terminal cost, so the route
alias itself is never used as the pricing identity when a served model was
observed.

When a classifier-enabled Switchyard route emits a structured classifier
record, the corresponding case result may also contain a
`classifier_evidence` object. It contains only sanitized routing facts:

```json
{
  "capability_boundary": "supported",
  "confidence": 0.92,
  "confidence_bucket": "very_high",
  "fail_closed_reason": null,
  "fallback_taken": false,
  "ingress_format": "openai_responses",
  "p_solve": 0.21,
  "selected_tier": "strong",
  "upstream_format": "openai_responses",
  "verdict": "selected_strong"
}
```

`verdict` distinguishes a classifier-selected strong tier from
`classifier_unavailable_fallback_strong`; the latter always sets
`fallback_taken` to `true` and includes a finite `fail_closed_reason`. The
confidence buckets are `low` (<0.50), `medium` (0.50–<0.75), `high`
(0.75–<0.90), and `very_high` (0.90–1.00); unavailable confidence is reported
as `unavailable`. Invalid or unavailable classifier records fail closed to the
strong tier. Prompts, model output, free-form rationale, error text, and
credentials are never copied into this object. A missing or ambiguous set of
records is left unattributed rather than guessed. The current live benchmark
contract remains passthrough-only; classifier fixtures exercise this report
path. Governed dispatch admits `switchyard/llm_classifier` only when
`inference_transport.allow_adaptive = true` and the route satisfies every
managed passthrough identity requirement: pinned binary and digest, immutable
route config, explicit expected version, and reviewed credential environment
names. The default is refusal, and each missed requirement is recorded with a
stable sanitized reason. This is a rollout-gated experiment; the rollout
decision remains **HOLD** pending human approval.

Every governed adaptive terminal event carries an `accounting_status`.
`reconciled` means the managed delta supplied a served identity and classifier
overhead, and the sanitized classifier-record count matches the proxy request
count when that count is available. Missing proxy metrics, served identity,
overhead, or classifier records produces `unreconciled` plus bounded reason
codes in the audit event.
The spend report renders **UNRECONCILED** prominently; any accompanying `$0`
is missing accounting evidence, not an attestation that provider usage was
free.

For the passthrough window, Kew:

1. takes an exclusive per-endpoint lock;
2. starts a fresh managed Switchyard process;
3. takes an initial snapshot, runs the direct corpus, then takes the baseline
   and requires proxy request counters to have stayed idle;
4. runs every passthrough case sequentially;
5. takes two final snapshots and again checks for overlapping traffic; and
6. terminates and reaps the owned process (and removes Codex sessions created
   by either leg).

The proxy evidence is valid only when exclusive use was proven and the request
counter delta exactly equals the corpus's expected total. A counter reset,
unexpected request count, or traffic outside the harness invalidates **all**
model, tier, latency, token, and other proxy deltas. Runner-authoritative usage
remains present so an invalid proxy window cannot be mistaken for missing
runner accounting.

## Exit codes

See the shared [exit-code scheme](exit-codes.md).

| Code | Meaning |
| --- | --- |
| `0` | Every runner execution and quality check passed, and proxy evidence is valid. |
| `1` | A run/check failed or the proxy observation window was invalid. |
| `2` | The request was refused before a report (invalid corpus/config, unsupported runner, unavailable binary/auth, proxy lifecycle failure, or lock contention). |

Treat code `1` as failed evidence, not as a result to average into a rollout
decision. Preserve the JSON only in the human-approved UAT evidence location
and follow that report's retention policy.
