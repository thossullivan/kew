# kew kill

Stops one run through its recorded process identity.

```text
Usage: kew kill [OPTIONS] RUN_ID
```

```bash
kew kill RUN_ID
```

The command records stop intent before reading the current run row.
Retries check that intent before starting another attempt.
The command authenticates the attempt's kernel token and signals its process group.
The shared termination helpers escalate surviving authenticated processes.
The command signals the owner only after proving the attempt group has exited.

The result reports authenticated signalling, an exited attempt, or unverifiable identity.
Unverifiable identity leaves stop intent recorded and returns exit code 2.
A missing run also returns exit code 2.

For a run with a container and no registered attempt, the message points at
[`kew settle`](settle.md), which reaps a run whose container never started.

The TUI worker kill action uses the same service.
Use [`kew stop`](stop.md) to stop the entire fleet and halt dispatch.
