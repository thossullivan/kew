# kew stop

Emergency stop for the whole fleet. Kills every in-flight agent run and
latches dispatch — nothing new starts until you run `kew resume`.

```text
Usage: kew stop [OPTIONS]

Options:
  --reason TEXT
```

```bash
kew stop
kew stop --reason "runaway spend on issue #42"
```

## What it does

1. Runs the same preflight as other GitHub-facing commands: `git` and `gh`
   must be installed, `gh auth status` must pass, and the repo must resolve.
   A failed check ends the command before the latch is touched.
2. Engages the fleet-stop latch, before any kill, so no new dispatches
   start while the kill proceeds.
3. For every in-flight run: records stop intent on the run so its worker
   cannot retry, verifies the recorded process group still belongs to a
   kew-launched agent (guards against PID reuse), sends `SIGTERM` to the
   process group, waits up to 10 seconds, and `SIGKILL`s survivors.
4. Marks a run `killed` in the run store only after its death is
   confirmed. A run whose death kew cannot prove stays pending as
   retained, and is named with a `retained:` warning giving the reason.
5. Resets affected issues from `in-progress` back to `ready`.
6. Preserves checkouts, so killed work is recoverable.
7. Records a `fleet_stop` event in the audit trail (`kew audit tail`).

Killed runs appear in `kew status` with status `killed` (not `failure`),
so operator stops are distinguishable from failures in history and
analytics.

## Options

| Option | Description |
| --- | --- |
| `--reason TEXT` | Recorded on the latch, shown by `kew status` and refusal messages |

## When `gh` is broken

Because the preflight runs first, an expired or missing `gh` login stops
`kew stop` before it engages the latch: it prints
`gh CLI not authenticated. Run: gh auth login` and kills nothing. Fix the
login (`gh auth login`, then `gh auth status`) and run `kew stop` again.
If `kew serve` is already running, its fleet stop
(`POST /api/fleet/stop`, an authenticated write) engages the same latch
and kills the same runs without the `gh` preflight; resetting issue labels
still needs a working `gh`.

## Notes

- Running `kew stop` with nothing in flight still engages the latch —
  that is the point of a circuit breaker.
- Running it twice is harmless; the second invocation reports the fleet
  is already stopped.
- There is **no** `--force` bypass on dispatch commands. `kew resume`
  is the only way back.
