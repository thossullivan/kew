# kew run-request

Queue a kew command for the running [`kew drive`](drive.md) to run as you — the
terminal half of the console's **Run** button.

```text
Usage: kew run-request [OPTIONS] COMMAND [ARGS]...

Commands:
  queue  Queue COMMAND for the running `kew drive`, which runs it as you.
  list   The newest run requests and what the drive did with each.
  show   One run request in full.
```

```text
Usage: kew run-request queue [OPTIONS] COMMAND...

Options:
  --item TEXT  The item the command acts on.
  --json       Print one JSON document instead of text.
```

```text
Usage: kew run-request list [OPTIONS]

Options:
  --json  Print one JSON document instead of text.
```

```text
Usage: kew run-request show [OPTIONS] REQUEST_ID

Options:
  --json  Print one JSON document instead of text.
```

The console prints the exact command that clears a row. Where that command is
one the drive may run, the block's **Run** button queues it instead of asking
you to paste it, and `kew run-request queue` queues exactly the same request:
the same ledger row, the same `run_request_queued` audit row, the same operator
(`KEW_ACTOR`, else your OS user).

The drive picks the request up and runs it as a fresh kew child with
`KEW_ACTOR` set to that operator, so the command writes the audit rows it would
have written typed in a terminal, and every gate inside it still applies. The
console process itself never launches anything.

**What can be queued.** A closed set of bounded commands, each argument one
plain token, no options:

- `kew dispatch N`
- `kew epic continue EPIC ITEM`
- `kew epic retry EPIC ITEM`
- `kew epic unhold EPIC ITEM`

Each of them authorizes agent spend, so queueing one is refused for an actor
outside `[approval].approvers` (an empty list admits anyone), as the console's
other spend verbs are.

Anything else — `kew epic run`, a command with options — stays a command to copy.

**What happens to it.** A request is `queued`, then `running`, then `done`
when the command succeeds or `failed` when it does not (or could not start),
with the tail of its output kept on the request. The drive `refused` it — recording why —
when it has waited in the queue for more than an hour (not counting time it
waited on a running local merge verify), when its operator is no
longer in `[approval].approvers`, or when the fleet is stopped, the drive is held
or the daily budget is spent. A request whose drive exited before it finished is
`unknown`, and is never run again. A `kew dispatch` the drive is about to start
is `superseded` instead when its item already has a run in flight, or a
dispatch run that started after the request was queued (the same command
copied and run in a terminal, say). The request names that run, and nothing is
started. Queueing a command that is already queued or
running returns that request rather than a second one — even one queued for
more than an hour. The drive refuses that one when it reaches it, and only then
does queueing the command again write a new request.

With no drive running, `queue` refuses rather than queue into nothing; a
command a request cannot carry is refused too. Nothing is written in either
case. Exit codes follow the [exit-code scheme](exit-codes.md).

**Example**

```bash
kew run-request queue kew epic unhold epic:checkout 12
kew run-request list
kew run-request show 20261010T120000123456-1a2b3c4d
```
