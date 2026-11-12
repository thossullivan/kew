# kew audit

Inspect the audit trail. Every governance action — dispatches, approval decisions, fleet stop/resume, CI fixes — lands as one row in the `audit_events` table of `dispatch.db`, carrying the full envelope: **who** (`actor_type`/`actor_id`), **which repo**, **which endpoint**, with `policy_hash`/`prev_hash` slots reserved for policy binding and hash chaining.

```text
Usage: kew audit COMMAND [ARGS]...
```

```text
Usage: kew audit export [OPTIONS]

Options:
  -f, --format TEXT
  -o, --output PATH
  --since TEXT
```

```text
Usage: kew audit verify [OPTIONS]

Options:
  --anchors PATH
  --json
```

```text
Usage: kew audit anchor [OPTIONS]

Options:
  --anchors PATH
```

```text
Usage: kew audit ledger-rebind [OPTIONS]

Options:
  --target PATH
  --expected-ledger-id TEXT
  --expected-anchor-hash TEXT
  --reason TEXT
  --yes
```

```text
Usage: kew audit policy [OPTIONS] [HASH]

Arguments:
  HASH

Options:
  --json
```

```text
Usage: kew audit tail [OPTIONS]

Options:
  -n, --limit INTEGER
  --event TEXT
  --item TEXT
  -f, --follow
  --json
```

```bash
kew audit tail                       # 20 most recent events
kew audit tail --limit 50 --event approval_hold
kew audit tail --item SFD-12         # only events for one work item
kew audit tail --follow              # poll for new events (Ctrl-C to stop)
kew audit tail --json                # one JSON object per line
kew audit export                     # full trail as JSON to stdout
kew audit export -f csv -o audit.csv
kew audit export --since 2026-06-01
```

Note: `-f` is `--follow` on `tail` but `--format` on `export` (matching the top-level `kew export`).

## Event types

| Event | Emitted by |
| --- | --- |
| `dispatch_completed` | every dispatch (success, failure, budget, auth failure, unreachable, exception) |
| `approval_hold` / `approval_granted` / `approval_confirmed` / `approval_declined` | the approval gate |
| `fleet_stop` / `fleet_resume` | `kew stop` / `kew resume` |
| `ci_fix_completed` | `kew ci-fix` |
| `circuit_breaker` | The automatic circuit breaker. Run scope includes cost anomalies, wall-clock limits, host resource caps, and unattributed pre-deadline 137 exits. A `hard_kill` can be an in-container SIGKILL or an unobserved OOM. Fleet scope includes daily and weekly spend breaches. The actor is `policy`. |
| `claim_rollback` | the scheduler, when a claimed issue can't be submitted to the pool and is released back to `ready` (`actor=policy`) |
| `epic_node_retry` / `epic_node_unhold` | `kew epic retry` / `kew epic unhold`. The unhold payload names the released hold reason and `review_cycle_restarted` — `true` when the release also granted the node a fresh reviewer-round allowance. The epic driver also emits `epic_node_retry` when it re-admits a node whose run failed as `resource`: the actor is `policy/driver_resource_retry`, and the payload adds `reason` (`resource`), `head` (the commit the failed run started from) and `run_id` (that run) |
| `epic_archived` / `epic_unarchived` | `kew epic archive` / `kew epic unarchive`. The payload carries `epic_ref`, the operator's `reason` (empty when none was given) and `source`. These two events ARE the archive state — the latest one per epic ref wins, so an unarchive outranks an archive rather than retracting it, and the whole history of a retirement stays readable here |

Dispatch and reviewer hard-stop events use the same payload fields.
They include stable `resource_cap_code` values.
They include the bounded `resource_cap_observations` list.
`resource_cap_observations_truncated = true` reports removed older entries.
The retry ladder stops for checkout quotas.
A confirmed OOM event reads as `resource` and is retried once: by the
dispatch's own retry ladder, or, for a sandboxed epic dispatch, by the
[epic driver](epic.md#automatic-resource-retries).
It retries host free-space and transcript caps after the configured backoff.
An accepted watchdog kill remains the cause when a competing OOM exists.
The competing OOM remains in the observations list.

An OOM probe records `oom_probe_status` as `seen`, `absent`, or `unavailable`.
An unavailable probe includes a bounded `oom_probe_diagnostic`.
These fields distinguish a successful empty query from a failed query.

## Who is the actor?

The actor is resolved per event: the `KEW_ACTOR` environment variable when set, otherwise the OS user. `KEW_ACTOR=alice kew stop` records `actor_id=alice`. Run rows record who *dispatched*; each audit event records who performed *that* action — the approver can differ from the dispatcher.

## Endpoint is configured, not attested

The recorded `endpoint` is what was *configured* at dispatch time
(`ANTHROPIC_BASE_URL` when set, else `anthropic:default`, for claude-code;
`<provider>:via-opencode` from the model's provider prefix for OpenCode; and the
fixed `openai:via-codex` for the official Codex runner) — honest, but not proof
of where traffic actually went. Credentials embedded in URL values (userinfo,
query strings) are stripped before recording. Endpoint attestation from runner
output is tracked separately (#251).

## Where did dispatch-log.jsonl go?

Retired. Its three writers now write `audit_events` rows instead — SQLite gives the audit trail one serialized, multi-process-safe write path and adds `run_id` to dispatch events. Existing `dispatch-log.jsonl` files are left on disk untouched; the per-run `.events.jsonl` telemetry stream is unchanged.

## `kew audit verify`

Verify the hash-chained audit log by recomputing every link from genesis —
not just the head. `--anchors PATH` additionally checks every in-range
anchored id against the recomputed chain, the anchored `count` against the live
chain (count-shrink relative to the latest anchor, bounded by
`[audit] staleness_window`), and the **anchor chain** — each anchor's `prev`
must bind the previous anchor. `--json` emits a machine verdict (including
`anchors.count_shrink` and `anchor_chain`).

It also prints how old the newest anchor is —
`Newest anchor: 2026-08-25T14:01:22.566429+00:00 (2.1 day(s) old).` — because
an intact chain says nothing about whether anyone is still anchoring it. The
age is advisory (it never changes the exit code), an unparseable stamp reads
`Newest anchor: unknown` rather than as fresh, and `--json` carries
`anchors.newest_timestamp` / `anchors.age_days`. The alarm on that age lives in
[`kew doctor`](doctor.md) (`[audit] anchor_max_age_days`, default 7 days).

The anchor-chain verdict reports how many links it actually verified —
`Anchor chain: intact (12 link(s) verified).` A legacy anchor file whose entries
predate the anchor chain verifies zero links, so more than one anchor with
`0 link(s) verified` prints a `WARNING`: the re-chain defence found nothing to
check rather than checking and passing (#1122). `anchor_chain.checked` and
`anchor_chain.total` carry the same numbers in `--json`.

`kew audit verify` already follows the shared [exit-code scheme](exit-codes.md).

Exit codes: `0` chain intact (and for an empty log) · `1` tamper detected
(broken link; an in-range anchor mismatch/absent row; count-shrink or a
stale/lagging anchor beyond the staleness window; or re-chained anchor history)
· `2` cannot run (missing or invalid ledger registration, missing database,
project/ledger mismatch, or an unreadable `--anchors` file).

Verification opens the selected project's registered ledger read-only through
the managed ledger boundary. It cannot create, register, or migrate a ledger.
An unregistered ledger, a registration whose database is gone, or a mismatch
causes exit `2`.

The verdict does not depend on which directory you stand in: the database and
the default anchors file both resolve against the project root, so `kew audit
verify` from a subdirectory reads the same ledger as from the root. (Before
#1156 it did not, and reading the wrong ledger reported a false `COUNT-SHRINK`
on a clean chain.)

See [Audit log & tamper-evidence](../audit.md) for what the guarantee does and
does not cover.

## `kew audit anchor`

Append the current chain head `{id, entry_hash, count, timestamp, prev}` to the
anchors file and print it — `prev` binds the previous anchor (the anchor chain).
The default is `audit-anchors.jsonl` **at the project root** (the directory
holding `kew.toml`), so running this from a subdirectory extends the one real
anchors file instead of dropping a stray copy beside you. A relative path passed
explicitly to `--anchors` still resolves against your current directory. Pure-local — no git, no network. Refuses to
extend a broken local chain or a re-chained anchor history.

Anchoring is a **read**: the database is opened read-only through the managed
ledger boundary, so an anchor run never creates, registers, or migrates a
ledger. The database it reads is the one the *selected project* configures —
`--project` / `KEW_PROJECT` first, then the discovered `kew.toml`. An
unregistered database, a registration whose database is gone, or a
project/ledger mismatch is exit `2` and writes no anchor.

The scheduled `audit-anchor` workflow runs this and commits/pushes the file; the
Actions run log is the witness. That workflow binds `KEW_PROJECT` alone (from
the repository variable of the same name), clears any inherited `KEW_LOG_DIR`, and never reconstructs a database path in shell.

## `kew audit ledger-rebind`

Explicitly recover or adopt one verified ledger. This command never merges,
splices, or reconciles two audit chains.

```bash
# Recover an identity-aware ledger with its complete canonical UUID.
kew audit ledger-rebind --target /path/to/dispatch.db \
  --expected-ledger-id 01234567-89ab-cdef-0123-456789abcdef \
  --reason "recover moved canonical ledger" --yes

# Adopt a legacy ledger with its latest anchor's full SHA-256 hash.
kew audit ledger-rebind --target /path/to/dispatch.db \
  --expected-anchor-hash <64-character-latest-anchor-hash> \
  --reason "adopt verified legacy ledger" --yes
```

`--target` selects the existing ledger database to register. Supply exactly
one complete identity with `--expected-ledger-id` or
`--expected-anchor-hash`. The command also requires a nonempty `--reason` and
the explicit `--yes` confirmation. It refuses identity or anchor mismatches
before changing the registry.

## `kew audit policy [HASH]`

Show the governance policy bound to dispatches, or resolve a retained policy hash.

- **No argument** — prints the *current* effective policy hash and its redacted
  snapshot (budgets, approval gate, routing, security patterns, limits, and the
  deployed security-hook digest).
- **`HASH`** — prints the retained snapshot and its `first_seen` timestamp for
  that policy. Exit 1 if the hash was never retained. Exit 2 applies when the
  database or its valid registration is unavailable or mismatched.

Hash resolution opens the selected project's registered ledger read-only
through the managed ledger boundary. It requires an existing registration,
matching database identity, and matching project path.

`--json` emits machine-readable output. Pure-local; no network. The snapshot is
a redacted **allowlist** — secrets such as `[alerting] webhook_url` are never
included.
