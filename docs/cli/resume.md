# kew resume

`kew resume` clears the fleet-stop latch that [`kew stop`](stop.md) engaged and
turns dispatch back on. Run it once you have looked at what went wrong and want
the fleet working again.

```text
Usage: kew resume
```

```bash
kew resume
```

Prints a confirmation and records a `fleet_resume` event in the audit trail.
Issues whose runs were killed are already labeled
`ready`, so the next `kew run` (or scheduler loop) picks them up again.

`kew resume` needs no GitHub access — it only touches the local
dispatch database, so it works even when `gh` auth is broken.

> **Not to be confused with `kew loop resume`.** `kew loop resume` continues a
> paused first-class loop (a single issue stopped at `needs_human` or
> `budget_limited`) with new operator guidance. See [`kew loop resume`](loop.md).
