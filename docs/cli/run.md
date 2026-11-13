# kew run

Take the next ready issue from the queue and dispatch the configured runner to implement it.
`kew run` works only from the ready queue. To dispatch one named issue, use
[`kew dispatch <issue>`](dispatch.md).

```text
Usage: kew run [OPTIONS]

Options:
  --drain         Keep dispatching until the queue is empty
  --dry-run       Preview issue selection without running
  --show-prompt   Print the generated prompt and exit
  --workers INT   Agents allowed in flight anywhere (overrides config)
  --force-budget  Bypass budget checks
  --json          Print one JSON result summary on stdout; progress stays on stderr
  --model TEXT    Override model for this dispatch
```

**Examples:**

```bash
# Single dispatch (pick next ready issue)
kew run

# Preview what would be picked
kew run --dry-run

# Drain the queue (exits after 3 empty checks)
kew run --drain

# Inspect the prompt that would be sent to the configured runner
kew run --dry-run --show-prompt

# Parallel dispatch with 3 workers
kew run --drain --workers 3
```

`--workers N` sets the concurrency cap for this invocation — the fleet-wide one,
[`[fleet] max_concurrent`](../configuration.md#fleet), which counts agents in
flight *anywhere*: on this machine and on any Compute Engine instance. It writes
both that key and `[parallel] max_workers`, so a configured fleet cap cannot
shadow the flag. Values below `1` are refused.

A held issue is released with [`kew dispatch <issue> --approve`](dispatch.md).
A run left awaiting host publication is repaired with [`kew finalize <run-id>`](finalize.md).

> **Note:** `run --drain` is *continuous dispatch* (drains the ready queue). It is
> unrelated to `kew dispatch <issue> --loop`, which supervises a single issue as a
> first-class, verified loop. See [`kew dispatch`](dispatch.md).

## JSON result summary

`kew run --json` prints one JSON document on stdout once the cycle ends, with
progress on stderr; an empty queue reports `"outcome": "empty"`. The shape is
the one [`kew dispatch --json`](dispatch.md#json-result-summary) prints.
`--json` is refused, as a configuration failure, alongside `--drain` or `--show-prompt`.

## `--drain` worker visibility

The scheduler excludes each issue that has a live child or pending run.
It counts live pending runs and reservations before it selects a batch.
An unprobeable pending run keeps its slot until its run finishes or the 24-hour stale-pending backstop reclaims it.
`kew cleanup` runs that same 24-hour sweep; it frees nothing younger than 24 hours.
The scheduler does not reap runs based on liveness verdicts.
The fleet-stop latch and the breaker are checked at the top of every pass, before any capacity wait.
The parent checks its own children every 2 seconds when they fill capacity.
It waits 60 seconds when the durable fleet count fills capacity.
After 10 consecutive such waits, and only while the loop owns no running child, it exits naming the holders, so a fleet held full by other actors never idles the loop until the 24-hour backstop.
It logs capacity holders and their liveness verdicts when the holder set changes.
A conflict-resolve worker occupies one slot: its pending run row once it exists, or the starting child before that.
A preview (`--dry-run` or `--show-prompt`) claims no slot and never waits: it keeps the durable count while slots remain and falls back to the local count only when the durable fleet is full.
Capacity waits preserve the empty-queue streak.
Three selection passes without a candidate end the loop, including passes with excluded candidates.
When ready candidates are hidden by a live child, a pending run, or a skip backoff, the messages name the held issues instead of asking for a `ready` label.
The held report reads the ready list once per no-selection pass, so a hidden number that is not a ready candidate is never reported as held.
An over-cap refusal restores the ready state without issue-specific backoff.
The parent waits 60 seconds before selecting again after an over-cap refusal; that wait runs after the fleet-stop and breaker checks.

A failed child keeps the runner's error category (for example `killed`, `timeout`, `needs_human`) and its reason names the attempt count and log path.
Each finished child prints its outcome category and reason:

```text
Worker #N exited with code X: <category> — <reason>
```

## Exit status

See the shared [exit-code scheme](exit-codes.md).

| Code | Meaning |
| --- | --- |
| `0` | Successful dispatch; empty queue; clean fleet drain; approval hold or declined estimate. |
| `1` | Runner or post-launch failure. |
| `2` | GitHub query failure; configuration failure; pre-launch failure; potentially stranded fleet claim. |

The fleet reports each potentially stranded claim after the drain, with its issue number and claim result.
`fetch_failed` and `race_detected` claims cause exit `2`, even when later checks find an empty queue.
The scheduler does not repair their labels.
`not_ready` and `already_claimed` remain warnings and allow exit `0`.
Raw runner codes remain in messages and run records.
