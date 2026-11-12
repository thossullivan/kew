# kew hosts

Build and inspect the machines kew ships verification jobs to. See
[Hosts](../hosts.md) for what a host holds, how to create one, its quota and
idle stop, and what it costs.

```text
Usage: kew hosts [OPTIONS] COMMAND [ARGS]...

Commands:
  provision  Run kew's checked-in provisioning recipe on a host and record its versions.
  release    Drop a named holder's lease on a host, by hand.
```

## kew hosts provision

```text
Usage: kew hosts provision [OPTIONS] NAME

Arguments:
  NAME  Host to provision: a hosts provider name, local or gcp  [required]

Options:
  --recipe TEXT  Recipe shipped with kew to run  [default: ubuntu-24.04.sh]
```

`kew hosts provision gcp` is experimental: it builds a fresh Ubuntu 24.04
instance with no hand fix, but its recipe and record may still change.

kew claims the host for the whole run, brings it up (cancelling any pending idle
stop), copies `kew/src/kew/hosts/provision/<recipe>` to it, runs it as root, and
releases it — which arms the idle stop, so a provisioning run cannot leave a VM
billing.

The recipe ends in a self-check that prints one `KEW_HOST_TOOL <name>=<version>`
line per tool and exits non-zero naming any tool it could not find, and any tool
whose version is not the one the recipe installs. On success
kew records those versions, with the recipe's name and sha256, beside the host's
lock at `~/.local/state/kew/hosts/<host>.provision.json` (under
`$XDG_STATE_HOME`, or `$KEW_HOST_LOCK_DIR`, when set). A failed run prints the log
and records **nothing** — a record is the claim "this host was built from this
recipe".

Only recipes shipped with kew resolve: a path, a parent reference or an unknown
name is refused before the host is touched, because the point of keeping the
recipe in the repository is that every host is built from one under review.

`local` is refused by name — it is this machine, and kew does not install system
packages on it.

**Exit codes** ([scheme](exit-codes.md)): `0` the host is provisioned and its
versions recorded; `2` it was not (unknown host, a recipe kew does not ship, an
unreachable instance, or a recipe that failed its own self-check). There is no
finding exit: a host kew could not reach and a recipe that failed its own
self-check are both "this host is not provisioned".

**Example:**

```bash
# Build the configured Compute Engine host from the Ubuntu 24.04 recipe
kew hosts provision gcp

# What kew recorded
cat ~/.local/state/kew/hosts/gcp-my-project-us-central1-a-verify-1.provision.json
```

## kew hosts release

```text
Usage: kew hosts release [OPTIONS] NAME

Arguments:
  NAME  Host whose lease to drop: a hosts provider name.  [required]

Options:
  --holder TEXT  The lease token to drop, as a refused job names it.  [required]
  --force        Drop the lease without stopping its job: nothing is stopped.
```

Drops the lease on the instance that names `--holder`, and only that one: a
lease held by anyone else is left alone. For a lease a dead kew process left
behind, which otherwise refuses every job (*still leased to another kew job
(held by TOKEN for …)*) until its deadline. A holder of this machine whose pid
is still running is refused — its job is live — unless it names an earlier boot;
when this machine's boot id cannot be read, a live pid is refused whatever boot
it names. So is a live pid in a token with no boot id at all, from a kew older
than the boot-id lease: its job left no scope record, so dropping its lease
would stop nothing. The job the holder left running on the instance is stopped in the same
locked command, whether or not its lease has expired — its whole scope, so a
process it detached is stopped with it, and the containers it started — so the
next job cannot ship over its checkout. A lease past its deadline that names
another holder is left alone, with or without `--force`. If a process of that job is
still running after the kill, or no scope record names the holder (a token from
a kew older than the job scope), the lease is not dropped and the command says
why (see [Hosts](../hosts.md)).

`--force` drops the lease without stopping anything, and says so: for a job you
know is gone, or accept may run on while the next job ships over its checkout.
It never drops the lease of a holder of this machine whose pid is still running.

**Exit codes** ([scheme](exit-codes.md)): `0` the lease is dropped (or the
instance is stopped, which clears it); `2` it was not (a live holder, a job not
proved stopped without `--force`, an unknown host, an unreachable instance, or
an instance that is neither running nor stopped — a suspended instance keeps its
memory, and the lease with it, so resume or stop it first).

**Example:**

```bash
kew hosts release gcp --holder my-mac-4242-3d89bcc0cba041d8b2dee1d0447da65c-555b6b49841f
```
