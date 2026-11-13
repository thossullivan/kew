# kew settle

Reaps one dead run whose container never started, and frees the slot it held.

```text
Usage: kew settle [OPTIONS] RUN_ID
```

```bash
kew settle RUN_ID
```

A contained dispatch whose owner died before its container started can stay
`pending`, holding a fleet and epic slot while its node reads as progressing.
kew's own reconcilers (admission and the epic tick) reap such a run
automatically. `kew settle` applies the same rule by hand, for one run.

The run is reaped only when all of this holds:

- its owner process is proven dead. An owner whose liveness is unknown is
  refused, never reaped;
- it owns no live continuation. A run claimed as the continuation of
  preserved work is refused, and kew names the continuation and the commands
  that move it, `kew epic continue` and `kew epic abandon`;
- the ten-minute startup grace has passed since the run started;
- no attempt was ever registered, and the run's log names no attempt identity;
- its container is `created` with pid 0, or the runtime says it does not
  exist. kew finds the container by its recorded id. With no id recorded, or
  with only the id the run's cidfile holds, kew accepts the container only
  when its `io.kew.ledger` and `io.kew.run` labels name this ledger and this
  run. A container labelled with another run, even a newer run of the same
  item, is never removed. "No such container" counts only from the daemon
  the run's launch recorded: after a context switch, or when the current
  daemon cannot be named, the run is refused. A run launched before kew
  recorded its daemon is judged as before.

When the rule holds, kew first records a `dead_run_container_removal_intended`
audit event naming the container id, its name, the runtime and the run. Then
it removes the container by its full id, without `-f`, so a container that
started in the meantime is kept. It finalizes the run as
`container_start_failed`, or keeps `killed` for a run that `kew kill` or
`kew stop` had already stopped, releases its reservation, and writes one
`never_started_run_reaped` audit event that names the operator.

If another process finished the run after kew removed its container, the run
stays as that process left it. kew records the removal in a
`dead_run_container_removed` event and prints that the container was removed.

Anything else changes nothing, and kew prints the reason. Either way the
command returns exit code 2. A missing run, or one no longer pending, also
returns exit code 2.

When [`kew kill`](kill.md) reports unverifiable identity for a run with a
container but no registered attempt, it suggests `kew settle`.
