# kew status

Show queue counts, run history, and cost summaries.

```text
Usage: kew status [OPTIONS]

Options:
  --history       Show recent run history
  --cost          Show cost summary
  --analytics     Show aggregate dispatch analytics
  --json          Emit machine-readable JSON
  --run TEXT      Show details for a specific run (add --json for the
                  machine-readable work view)
```

**Examples:**

```bash
# Queue counts
kew status

# Recent run history with outcomes
kew status --history

# Cost breakdown by runner and model
kew status --cost

# Aggregate dispatch analytics
kew status --analytics

# Machine-readable JSON
kew status --json
```

The queue counts include the configured approval-hold label (default
`needs-approval`), so you can see at a glance how many issues are parked
by the [approval gate](../configuration.md#approval) awaiting release.

Each count is the real number of open issues carrying that label, up to 1000.
A count that reaches 1000 prints as `1000+`, followed by a line saying it
means at least that many: kew asks GitHub for at most 1000 issues per label.

## Tracker read failures

See the shared [exit-code scheme](exit-codes.md).

Both human output and `--json` exit with code 2 after a required GitHub
tracker read fails. The JSON path writes the failure to stderr and writes no
JSON to stdout.

The human path prints queue counts only after every `gh issue list` call
succeeds. It never combines successful counts with fallback zeros.

A `gh` exit of 0 with an empty or non-JSON body counts as a failed read, not as
an empty queue. On a transient GitHub error (rate limit, connection reset) the
read retries with backoff for up to about three minutes before exiting 2.

## Fleet stopped banner

While the fleet is stopped (see [`kew stop`](stop.md)), `kew status`
prints a prominent `FLEET STOPPED (reason, timestamp)` banner above the
normal output, and the [TUI](../tui/index.md) shows the equivalent
banner in its status bar. Run [`kew resume`](resume.md) to clear it.

## Single-run detail (`--run`)

`kew status --run <run-id>` shows one run, then the **canonical work
projection** for the item that run belongs to — the same snapshot-bound view
the console and the API read, so all three agree about what is happening and
what to do next:

```text
Run:      r1
Item:     SFD-12
Branch:   agent/issue-sfd-12
Status:   awaiting_publication
...

Revision:  wv1:31341705ce06ca0b8a540e616017c0ef
Execution: completed (awaiting_publication), attempt 1 of 1
Artifact:  preserved (publication pending)
Judgment:  not_applicable
Source:    unknown (sync unknown)
Route:     claude-code / claude-opus-5 — claim unverified (declared)
Exception: awaiting_publication — implementation preserved on agent/issue-sfd-12
Evidence:  pull_request stale/cached (412s old)
           tracker_item unavailable/unobserved (never read)
Next:      publication-finalization: finalize_publication — publish that same
           artifact rather than running the work again
Actions:   publication-finalization: finalize_publication -> run r1
```

Reading the block:

- **Item** is the tracker's own id — `#42` for GitHub, `SFD-12` for Linear.
  It is never degraded to `#0` for a tracker that does not number its items.
- **Revision** (`view_revision`) digests the facts behind the view, never the
  clock. Two surfaces showing the same revision are showing the same world.
- **Route** is a *claim*: `unverified` means nothing came back from the runner
  to check the declared model against, and `substituted` means the runner
  reported serving a different one.
- **Evidence** ages every remote reading. `unavailable` means the read did not
  happen or failed — which is **not** "there is nothing there".
- **Next** is the one safest action, prefixed by the kind of decision it is:
  `admission` (start work), `independent-review` (a red build or a conflict),
  `human-release` (someone must decide), `publication-finalization` (publish
  work that is already preserved), or `address-feedback` (a reviewer asked for
  changes). `[blocked by <dimension>]` means the recommendation is to go and
  look, because the fact the decision rests on could not be read.
- **Actions** lists only what kew itself can safely be asked to do. It is
  empty when the next step belongs to a person — and preserved work is offered
  finalization, never a fresh dispatch that would re-do it.

### Machine-readable form

```bash
kew status --run <run-id> --json
```

Emits JSON only (no banner), with a stable top level:

| Key | Meaning |
| --- | --- |
| `run` | the run row (same fields as `--json`'s `runs` entries) |
| `item_id` / `item_ref` | the opaque tracker id and its display form |
| `view_revision` | the revision to pin when acting on this view |
| `composed_at` | when the view was composed (UTC ISO-8601) |
| `healthy` | `false` for any exception **and** for any reading that is not current |
| `route` | the route claim (`runner`, `model`, `reported_model`, `claim`) |
| `safest_action` | the one next action, its `reason`, `blocked_by` and `stage` |
| `actions` | every action kew may safely be asked for (may be empty) |
| `evidence` | each observation with `availability`, `provenance` and `age_seconds` |
| `work` | the full work view, all dimensions |

An unknown run id keeps its scriptable contract in both modes: a message on
stderr and exit code 2, never an empty-but-successful document.

While GitHub is unreachable the view does not go quiet or green: the pull
request reads as `stale`/`cached` (the last persisted reconciliation) or
`unavailable`/`unobserved`, `healthy` is `false`, and the safest action becomes
`observe_remote` rather than a guess.

## Exit codes

See the shared [exit-code scheme](exit-codes.md).

| Code | Meaning |
| --- | --- |
| `0` | Status read successfully; empty queue. |
| `2` | Unknown run; GitHub query failure; configuration failure; invalid request. |
