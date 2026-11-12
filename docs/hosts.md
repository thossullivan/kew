# Hosts

A **host** is a machine kew ships a git bundle to and runs *one* job on — a
verification run for a merge candidate, for example. `local` is the default and
needs no cloud account at all; `gcp` is a Compute Engine instance driven through
the `gcloud` CLI. The settings live in [`[hosts]`](configuration.md#hosts).

`kew hosts provision gcp` and `kew verify --host gcp` (sharded with `--shards`
or not) are experimental: they work on a fresh Ubuntu 24.04 instance, but expect
them to change.

## What a host holds

A host holds **tools and code**. The one credential that ever reaches one is
the model credential of an agent **dispatch** placed there, which lives in that
container's environment and nowhere else — see
[the credential exception](#the-credential-exception).

| On a host | Not on a host |
| --- | --- |
| Docker, Node 22, uv, git, ripgrep, ruby, perl, jq, lsof, an ssh client, the pinned trivy and gitleaks, the Chromium headless shell | GitHub, Linear, Google or cloud credentials |
| The checkout cloned from the git bundle kew ships | kew's config, its ledger, its audit chain |
| The provisioning recipe kew copied there | Anything that could push, comment, or label |

Code goes in as a git bundle. Results come back as **data** — an exit code, a
wall-clock duration, and log text. Nothing a host returns is treated as an
instruction: the versions on this page are read off the recipe's output with a
line-shaped regex, stored, and printed. The job environment is an allowlist, and
a declared variable whose *name* reads like a credential (`*TOKEN*`, `*KEY*`,
`GITHUB_*`, `CLOUDSDK_*`, …) is refused by name even when a caller asks for it —
every one of them, for a job, and all but the fenced exception below for a
placed agent.
See [Security](security.md#verification-hosts) for the boundary this sits
inside, and [`[hosts]`](configuration.md#hosts) for what a job receives.

One job runs at a time per host. The mutual exclusion is kew's own — a
`flock`-held lock file named for the instance — because two jobs shipped to one
instance would clone over each other's checkout. A second kew process that
wants a busy host is told so; it does not reach in.

## The recipe

Every host is built from one file in this repository:

```text
kew/src/kew/hosts/provision/ubuntu-24.04.sh
```

It installs Docker (from Docker's own apt repository), Node 22 (from
NodeSource), uv (from the release archive pinned in `trivy.pin`, see below),
git, and `playwright-core` **pinned to 1.63.0** with the
Chromium headless shell that `ui-check` drives. The pin is the browser line the
console's screenshots were taken against; everything else tracks its
distribution. Browsers are installed under `/opt/playwright-browsers` so every
login can read them, not into the provisioning user's `~/.cache`.

It installs **ripgrep**, **ruby**, **perl**, **jq**, **lsof** and an **ssh** client from
the distribution too, and **gitleaks** at the exact version
`scripts/verify.sh` and CI pin (from the release archive, checked against a
pinned SHA-256 on every run: an installed gitleaks that merely reports the
pinned version is replaced, never trusted), because tests in the suite skip without them — and
`verify.sh` exits without gitleaks — while a merge verify run with
`KEW_REQUIRE_TOOLS=1` refuses such a skip. The list of tools tests need lives
in `kew/tests/support/required_tools.py`; a policy test fails if the recipe
does not install and self-check every one of them, and fails if a test looks
up a tool with `shutil.which` that the list neither requires nor says where a
host gets it, or whose name it cannot read off the source (a literal, a
literal loop, a parametrization or a module constant).

It also installs **trivy**, the image scanner the gate's last step *is*, at the
exact version that gate pins — read from one file beside the recipe, never
restated in it:

```text
kew/src/kew/hosts/provision/trivy.pin
```

`scripts/scan-agent-image.sh` reads its pinned version out of that same file and
refuses any other one, because a scanner of a different version is a different
gate. The release archive's SHA-256 is pinned there too and verified before
anything is extracted, and the recipe installs from that verified archive on
every run: a version string is self-reported, so a trivy that merely answers
the pinned version is replaced, never trusted. The SHA-256 of the trivy binary
inside each pinned archive is in the file as well, and the gate refuses any
installed trivy whose binary does not match it — a Homebrew trivy of the same
version included. Advance the pin by editing that file alone.

uv is pinned in the same file: its version and the SHA-256 of the two Linux
release archives the recipe may install it from. The archive is downloaded and
checked before `uv` and `uvx` are extracted; the recipe never runs the
astral.sh installer script.

With uv in place the recipe runs `uv python install 3.13`, the Python CI tests
on, and `uv python pin --global 3.13` as the login that ran it through `sudo`
(`SUDO_USER`, or the recipe's own user when it was not run through `sudo`),
the login every job runs as, so a job's `uv sync` builds on that Python and not
on the 3.12 Ubuntu ships. Both are per login, in that login's home: a job that
arrives as any other login gets neither the managed Python nor the pin, so run
the recipe through `sudo` as the login kew connects as, which is what
`kew hosts provision` does. It also writes
`/etc/systemd/logind.conf.d/kew-keep-ipc.conf` with `RemoveIPC=no` and reloads
logind: kew ships the bundle to `/dev/shm/kew-host` in one ssh session and
clones it in the next, and logind would otherwise delete a login's `/dev/shm`
files when its last session ends.

Each install step is idempotent, so re-running the recipe on a provisioned host
is a no-op that ends in the same self-check:

```bash
sudo bash ubuntu-24.04.sh          # install everything, then self-check
bash ubuntu-24.04.sh --self-check  # the self-check alone; needs no root
```

The self-check prints one line per tool and **exits non-zero naming every tool
it could not find, and every tool whose version is not the one this recipe
installs** — Node 22, `playwright-core` 1.63.0 and the pinned trivy are checked
against the recipe's own pins, and `docker` is asked for its *server* version,
which a client with no reachable daemon cannot answer. `python` is the Python
the job's login gets from `uv python find` outside any project, held to 3.13,
and `logind-keep-ipc` is systemd's version, reported only while the
`RemoveIPC=no` drop-in is in place:

```text
KEW_HOST_TOOL git=2.43.0
KEW_HOST_TOOL docker=27.3.1
KEW_HOST_TOOL node=22.9.0
KEW_HOST_TOOL npm=10.8.3
KEW_HOST_TOOL uv=0.5.11
KEW_HOST_TOOL python=3.13.5
KEW_HOST_TOOL trivy=0.69.3
KEW_HOST_TOOL playwright-core=1.63.0
KEW_HOST_TOOL chromium-headless-shell=1200
KEW_HOST_TOOL ripgrep=14.1.0
KEW_HOST_TOOL ruby=3.2.3
KEW_HOST_TOOL perl=5.38.2
KEW_HOST_TOOL jq=1.7.1
KEW_HOST_TOOL lsof=4.95.0
KEW_HOST_TOOL ssh=9.6
KEW_HOST_TOOL gitleaks=8.30.0
KEW_HOST_TOOL logind-keep-ipc=255
kew host self-check OK: 17 tools
```

A half-provisioned host — and a host carrying someone else's Node, or a dead
`dockerd` — therefore fails here, loudly, instead of failing a verification run
an hour later. The self-check uses bash builtins only — no `grep`, no `awk` —
so it can be driven with nothing but the tools under test on `PATH`, which is
how kew's own tests prove it fails when one is absent or off the pin. The one
exception is asking uv as the job's login, which takes `id` and `env`, and
`sudo` when the recipe runs as root for another login.

### The PATH a job gets

A job does not arrive in a login shell. It arrives as
`gcloud compute ssh --command`, which runs a non-interactive, non-login shell:
no profile is sourced, and kew forwards none of its own environment to a host
whose directories are not this machine's. So kew gives every remote command a
known PATH of its own before it runs:

```text
$HOME/.local/bin:/usr/local/bin:/usr/local/sbin:/usr/bin:/usr/sbin:/bin:/sbin
```

The first two entries are where the recipe installs what apt did not. Anything
the host's own shell already had is kept after them, and `$HOME` is expanded by
the host, not by kew.

The self-check looks for every tool on **that** list — not on the PATH of
whoever ran the recipe — which is what keeps the two answers from disagreeing.
A host whose uv is reachable only from a login shell fails provisioning, and
`kew hosts provision` passes the SSH login's own expanded value in as
`KEW_JOB_PATH` so the check runs against the job's home rather than root's.
Before this, a host provisioned cleanly and then failed *every* job in under
two seconds with `uv: not found`.

## How to create one

1. **Create the instance.** Any Ubuntu 24.04 image will do; the size below is
   the one the cost example assumes.

    ```bash
    gcloud compute instances create verify-1 \
      --project my-project --zone us-central1-a \
      --machine-type e2-standard-4 \
      --image-family ubuntu-2404-lts-amd64 --image-project ubuntu-os-cloud \
      --boot-disk-size 100GB --boot-disk-type pd-balanced
    ```

2. **Point kew at it** in `kew.toml`:

    ```toml
    [hosts]
    provider = "gcp"

    [hosts.gcp]
    project = "my-project"
    zone = "us-central1-a"
    instance = "verify-1"
    idle_stop_minutes = 30
    ```

3. **Check kew can reach it.** [`kew doctor`](cli/doctor.md) probes the host in
   dependency order — `gcloud` on `PATH`, a usable login, the instance
   reachable — and prints the command that fixes each failure. An expired login
   is the common one.

4. **Provision it** with [`kew hosts provision`](cli/hosts.md):

    ```bash
    kew hosts provision gcp
    ```

    kew claims the host, brings it up, copies the recipe to it, runs it as root,
    and releases it — which arms the idle stop, so a provisioning run cannot
    leave a VM billing. On success the versions the self-check reported are
    recorded beside the host's lock, as
    `~/.local/state/kew/hosts/gcp-<project>-<zone>-<instance>.provision.json`
    (under `$XDG_STATE_HOME` when that is set, or `$KEW_HOST_LOCK_DIR`):

    ```json
    {
      "host": "gcp",
      "lock_key": "gcp-my-project-us-central1-a-verify-1",
      "provisioned_at": "2026-10-06T12:00:00+00:00",
      "recipe": "ubuntu-24.04.sh",
      "recipe_sha256": "…",
      "seconds": 412.7,
      "versions": { "docker": "27.3.1", "node": "22.9.0", "…": "…" }
    }
    ```

    The record names the **sha256 of the recipe**, not just its file name, so
    you can tell which revision of the recipe built a host. A failed run leaves
    *no* record: a record is the claim "this host was built from this recipe",
    and a half-finished run has no business making it.

### A host that runs agent containers

A verification host runs `pytest`. A host an **agent** run is placed on
(`[sandbox] host = "gcp"`, see
[Configuration](configuration.md#sandbox-host)) runs the same contained
dispatch container this machine would run — and that container attaches the
egress network `[sandbox] network` names, `kew-allowlist` by default. The
recipe creates `kew-allowlist` when it is absent, in the same shape as on your
own machine: a plain bridge (driver `bridge`, not internal, no options). A
network that already exists is left as it is, so re-provisioning never undoes
restrictions you have put on it. On an instance provisioned before the recipe
did this, create it by hand once:

```bash
gcloud compute ssh verify-1 --project my-project --zone us-central1-a \
  --command 'docker network create --driver bridge kew-allowlist'
```

**Egress on it is operator-managed**, as on your Mac: kew installs no firewall
rules on it. A plain bridge has **unrestricted egress** while the container
holds your model credential, so restrict it to your inference endpoint and
GitHub before placing work, exactly as on this machine (see
[Getting started](getting-started.md)). A network with a custom
`[sandbox] network` name is never created for you. `[sandbox] network =
"open"` is the explicit opt-out and consults no network at all.

kew probes the network **on the instance before it launches an agent there**
and refuses the placement naming that command when it is absent, rather than
leaving the instance's own daemon to refuse the container after the bundle, the
clone and possibly an image build. A probe that failed because there was no
daemon to ask — an instance that cold-started seconds ago, or a login that is
not in the `docker` group — is reported as that, with the client's own words,
and not as a network to create. The pinned agent image is the other
prerequisite, and that one kew can supply: an instance without it builds it
from **this machine's packaged image inputs** — the `docker/` context installed
with kew, checked against its manifest and digest pin and streamed to the
instance's `docker build -` over the session. An instance whose image under
that tag lacks the `dev.kew.agent-image-inputs` label for those inputs (built
before the label existed, or retagged) rebuilds it the same way, and a rebuild
that still lacks the label refuses the placement
([Security](security.md#a-pinned-image-tag-is-checked-against-the-images-inputs-label)). Nothing in the shipped checkout
is executed on the instance's host: the clone itself runs in an emptied
environment (no `GIT_*` variable from the login, so no command-scope git
config), with system and global git config ignored, the LFS driver emptied
and hooks off, so the branch's own `.gitattributes` cannot choose a filter
that runs there.

Only **kew's packaged image** is placed. The credential handoff starts the
image's entrypoint itself, and the one it starts is read from kew's packaged
inputs, so a custom `[sandbox] image` would have its own entrypoint silently
replaced; a placement naming any other tag is refused before anything is
shipped.

### How many agents, and where

Two caps, read off two different things:

- **The fleet cap** — [`[fleet] max_concurrent`](configuration.md#fleet),
  defaulting to `[parallel] max_workers`. Counted off the **ledger**: every run
  in a running state, whatever host it sits on. VMs *replace* local slots rather
  than add to them, so three concurrent agents stays three whether they are all
  here, all on the cloud, or split across both. The fourth waits whichever host
  it would have gone to.
- **Per-host capacity** — [`[hosts.gcp] max_runs`](configuration.md#hosts),
  `1` per instance, which is what a host honours: a job holds its instance for
  the whole session and jobs on one machine share one checkout, so the config
  refuses a higher number rather than let a placement spend capacity the
  instance would refuse. It is what makes a `prefer-gcp`
  [placement](configuration.md#sandbox-host) able to tell "the cloud has room"
  from "the cloud is full": with every instance at `max_runs`, `prefer-gcp`
  places on this machine, while a `gcp` pin waits rather than quietly undoing
  the operator's decision.

`kew status` prints an `In flight:` block naming the host and instance of every
in-flight run, and the console's route line carries the same two facts. A
placement is a recorded fact, read from the ledger — neither surface goes and
asks a VM. What records it is the placement itself: the host and instance are
written to the run's row on the way in, before its container starts, because the
ledger's default is `local` and a column nobody wrote would read as *every
instance is free*.

### A pool of hosts

More than one host is a *pool*, and a pool is what
[`kew verify --shards N`](cli/verify.md) spreads the verification's parallel
test phase across. List them in order; kew takes the first N, and the first of
them also runs everything that runs once — the static gates, the serial phase,
`ui-check`:

```toml
[hosts.gcp]
project = "my-project"
zone = "us-central1-a"
instances = ["verify-1", "verify-2", "verify-3"]
idle_stop_minutes = 30
```

Every host in the pool is built from the same recipe, by the same command: a
pool is three ordinary hosts, not a special kind of one. `instance` defaults to
the pool's first entry, so an unsharded verification keeps running on
`verify-1` without it being named twice. The same instance listed twice is
refused — one host runs one job at a time, so two shards aimed at it would
queue behind each other rather than halve anything. Price a pool before you
build it: it multiplies the **disk** line, not the CPU line (see
[What a host costs](#what-a-host-costs)).

`kew hosts provision local` is refused by name. The local host is *this
machine*, and kew does not install system packages on it; run the recipe
yourself if that is what you want.

`gcloud` is an **optional external CLI**, like `docker` and `gh`: probed at use,
reported by `kew doctor`, never a Python dependency. With no `gcloud` installed
the gcp host is simply unavailable and `local` is unaffected.

## Placing agent runs

A verification job is a `pytest` invocation. An **agent run** — a dispatch, a
review, a remediation — is a contained agent container with a model credential
inside it, so placing one on an instance is the one host operation that carries
a credential to another machine. This section is the operator runbook for that:
how to place runs, the one credential exception, what a pre-empted spot
instance looks like from here, and how to get back to `local`.

!!! note "What `placement` reaches"

    A `dispatch` run placed on `gcp` runs its agent container on that instance:
    [`kew dispatch`](cli/dispatch.md) and the epic driver go through the same
    launch, and `kew status` names the instance each in-flight run is on.
    Every other run kind — a review, a remediation, a resolve, a CI fix — is
    never placed: it is excluded from the one credential exception below, so
    there would be nothing to authenticate it with on a VM. Under `prefer-gcp`
    a contained remediation, resolve or CI fix runs here; a `gcp` pin that
    governs it — `placement = "gcp"`, or a `[sandbox.hosts]` entry for its
    kind — **refuses** it before it is admitted, because a pin is not a
    preference. Pin only `dispatch` to keep the others running. **Publication never moves.** The agent's work comes
    back as a verified artifact and the branch, the PR and the report are
    pushed from here, the only machine holding a GitHub credential. `local` is
    the default and the only setting that needs no cloud account.

    Three kinds of dispatch cannot be placed whatever the policy says: one on
    [`[sandbox] runtime = "openshell"`](configuration.md#sandbox), whose
    sandbox is created through the gateway on this machine and so has no
    container for an instance's daemon to run; one whose inference transport
    runs a sidecar proxy beside its container (that process is on this machine
    and a container on an instance has no route to it); and one with no model
    credential for the launch to hand over, because the credential is
    forwarded from here and there is nothing else a VM may receive. Under
    `prefer-gcp` any of them runs here; under a `gcp` pin any of them
    **waits**, because a pin is not a preference.

### How to place runs

Four keys in `kew.toml`. The cap decides *whether* a run starts at all; the
policy and the pin decide *where* it goes, and a pin outranks the policy:

```toml
[sandbox]
placement = "prefer-gcp"   # "local" (default) | "gcp" | "prefer-gcp"

[sandbox.hosts]
dispatch = "gcp"           # a per-kind PIN, which outranks the policy for that kind

[fleet]
max_concurrent = 3         # the whole fleet, this machine and the VMs together

[hosts.gcp]
project = "my-project"
zone = "us-central1-a"
instances = ["agent-1", "agent-2"]
idle_stop_minutes = 30
```

- **The policy** — [`[sandbox] placement`](configuration.md#sandbox-host).
  `prefer-gcp` takes the first instance with room and this machine when every
  one is at [`max_runs`](configuration.md#hosts); it is the policy to use when
  the point is to get load off the laptop. A `gcp` pin **waits** for an
  instance instead, because an explicit pin is not a preference. `local`
  places here and consults no cloud at all.
- **The pin** — [`[sandbox.hosts]`](configuration.md#sandbox-host), keyed by
  run kind (`dispatch`, `adversarial_review`, `review_remediation`, `ci_fix`,
  …). A kind named there is pinned and ignores the policy; a key naming no run
  kind kew runs is refused when the config loads, because an override is read
  by exact key and a typo would otherwise leave that kind where it was with
  nothing said.
- **The cap** — [`[fleet] max_concurrent`](configuration.md#fleet). Do **not**
  reduce this machine's slots by hand to make room for VMs: the cap is counted
  off the ledger — every run in a running state, whatever host it sits on — so
  three concurrent agents stays three whether they are all here, all on the
  cloud, or split across both. VMs replace local slots rather than add to
  them.
- **The pool** — [`[hosts.gcp]`](configuration.md#hosts). `max_runs` is `1`
  per instance and config refuses more, because a job holds its instance for
  the whole session and every job on one machine shares one checkout. That cap
  is what lets `prefer-gcp` tell "the cloud has room" from "the cloud is
  full". An instance another kew job is holding — a sharded verify shard, a
  remediation, a dispatch in another process — counts as full too, even with
  no dispatch recorded on it, so the run waits or comes here before it is
  admitted rather than failing on that instance afterwards. Before a pool
  that reads full is refused, kew finalizes every in-flight row whose owning
  kew process is provably dead — the sweep admission itself runs — so one
  crashed run cannot hold its instance full for good. A row whose owner kew
  cannot prove dead still counts.

There is no flag and no environment variable for the policy: it is one line in
`kew.toml`, and the next placement reads the file.

Where a run actually went is a **recorded fact**, not a question anyone asks a
VM: the host and the instance are written to the run's row on the way in,
before its container starts, and [`kew status`](cli/status.md) prints an
`In flight:` block naming both for every in-flight run. The console's route
line carries the same two facts, and the launch writes them to the audit chain
as well.

### The credential exception

An instance holds no GitHub, Linear, Google or cloud credential: a *job*'s
environment is an allowlist, and a declared variable whose **name** reads like
a credential is refused by name even when a caller asks for it (see
[Security](security.md#verification-hosts)).

The one exception is the runner's own declared model credential — for
`claude-code`, `CLAUDE_CODE_OAUTH_TOKEN` or `ANTHROPIC_API_KEY`, whichever the
run is routed with. It is fenced four ways, and a refusal names which fence it
hit:

| Fence | What it refuses |
| --- | --- |
| Run kind | Everything but a `dispatch` run. A review or remediation placed on a VM gets no credential at all, so the exception cannot widen by a caller renaming what it is doing |
| Declaration | Every name the runner does not itself declare — another runner's model credential included, and anything GitHub-shaped |
| Name shape | A name that is not shell-usable: it is interpolated into the remote prelude, where a name, unlike a value, cannot be quoted |
| Empty value | A declared name requested blank, which authenticates nothing — refused here rather than failing deep inside the agent |

It reaches **that run's container environment only**. It is never written to
the instance's disk and never appears on a command line: it travels on the SSH
session's stdin, is held unexported in the remote shell's memory, and is piped
to the **container's own stdin**, where a small handoff shell exports it and
starts the image's entrypoint. It is never passed as `docker run --env`, which
would make the daemon store the value in the container's config on the
instance's disk — `docker inspect` of a placed container shows no credential
value. The container imports **exactly** the names that were handed over —
the ones the audit event below lists. Every other declared credential name is
dropped from the command line and unset in the remote shell first, and every
proxy name not handed over is pinned blank, so nothing the instance's own login
environment exports can stand in for a credential the operator never sent. The value it carries is the one a contained run on this machine
would receive — for an OpenRouter-backed runner that is the run's own bounded,
spend-capped key, never the operator's ambient `OPENROUTER_API_KEY`.

And the launch records it **by name** — a `dispatch_remote_launch` audit event
([`kew audit`](cli/audit.md)) whose payload names the host, the instance, the
image, the runner and the credential names:

```json
{
  "event": "dispatch_remote_launch",
  "actor_type": "host",
  "actor_id": "agent-1",
  "payload": {
    "host": "gcp",
    "instance": "agent-1",
    "image": "kew-agent:docker-d148816b388f",
    "runner": "claude-code",
    "credentials": ["CLAUDE_CODE_OAUTH_TOKEN"],
    "image_built": false,
    "image_id": "sha256:0f4d2e6a8c1b3d5f7e9a0c2b4d6f8a1c3e5b7d9f0a2c4e6b8d0f2a4c6e8b0d2f"
  }
}
```

`image_id` is the immutable ID **the instance** resolved that tag to, read
before its container starts, and it is what the run's containment profile
records. The tag resolves against the instance's own image store, so this
machine's ID for the same tag names bytes no container there ever reads.

Publication is not on that list and cannot be. The instance returns the
finished checkout as an **artifact**, and before any of it is written into
this machine's checkout it passes the same output secret scan a local
publication runs: work that carries a credential this machine holds is
refused as `remote_host_artifact_refused`. The scanned tree is then written
out without git's checkout machinery: kew streams each changed blob's raw
bytes to disk itself, keeping only the executable bit and symlinks the tree
records, and refuses any path that is unsafe, passes through a symlinked
directory, or names the same file as another path on a case-insensitive or
normalising filesystem (`README.md` beside `readme.md`). A file the work left
unchanged is checked too: if the base checkout converted its bytes (a base
`*.txt eol=crlf`), it is rewritten from its blob, so every file the tree
records is on disk as exactly that blob. No smudge filter, LFS fetch, `.gitattributes` conversion (`eol`,
`ident`, `working-tree-encoding`), hook or fsmonitor runs, so the bytes on
disk are the bytes the scan read. Every git command on that path runs with
hooks, fsmonitor and attribute files off and no system or global config. The
checkout's index is left as it was. This machine's existing publication
path — the filter, the protected paths, the broker push — then publishes it
unchanged. Nothing on a VM can push or merge.

### Spot instances, and what a pre-emption looks like

An agent host is a good spot candidate: the work is re-askable, and a
pre-empted run costs its own minutes and nothing else. Create one with the
termination action **named** rather than defaulted — kew's idle stop, and the
start before the next job, both expect a machine that can be stopped and
started again:

```bash
gcloud compute instances create agent-1 \
  --project my-project --zone us-central1-a \
  --machine-type e2-standard-4 \
  --provisioning-model=SPOT --instance-termination-action=STOP \
  --image-family ubuntu-2404-lts-amd64 --image-project ubuntu-os-cloud \
  --boot-disk-size 100GB --boot-disk-type pd-balanced
```

From this machine a pre-emption is **indistinguishable from a lost SSH
session, and is reported as the same thing**: the instance goes away mid-run,
the relayed event stream stops, and no exit marker arrives. kew reports that
as an *interruption* — error category `remote_host_interrupted` — never as a
clean zero and never as a failed gate, because `gcloud`'s own exit status
cannot tell a failed agent from a failed connection:

```text
the session to agent-1 ended with no exit marker after 143 relayed event
line(s) (ssh exit …), so the run was interrupted and is NOT a success
```

Three things follow without an operator doing anything, and together they are
the recovery:

- **The container is stopped and its name freed.** A dropped session ends only
  the local `ssh` process group; the container belongs to the instance's
  daemon and would otherwise keep both the CPU and the `kew-<slug>` name the
  next attempt asks for. Every placement also sweeps that name *before* it
  launches, so an operator process that died with its run leaves nothing for
  the daemon to refuse.
- **This machine's checkout is untouched.** The run was bundled out of it,
  read-only, and an interrupted placement returns no artifact — there is
  nothing half-applied to undo. The work an interruption asks for is the run
  again.
- **The instance comes back by itself.** A stopped spot instance is startable,
  and the next placement starts it, waits out the login refusal of a cold
  start, and proves the pending [idle stop](#idle-stop) was cancelled before
  it ships anything.

A run whose *operator process* died with it leaves a `pending` row behind, and
that row would hold its instance up for ever — nothing on this machine can
prove a container on another one dead. The idle-stop question is therefore
age-bounded at twice the agent's own `[agent] timeout_minutes` ceiling, so a
stranded row stops protecting the machine rather than billing for it.

### Falling back to local

One line, and the next placement reads it:

```toml
[sandbox]
placement = "local"
```

Nothing else has to change: `local` needs no cloud account, no `gcloud` and no
instance, and it is what an unset `placement` already reads from `host`. A
per-kind pin is the smaller version of the same move —
`[sandbox.hosts] dispatch = "local"` brings implementation runs home while
everything else keeps whatever the policy says. Three signs it is time:

| Sign | What it means |
| --- | --- |
| [`kew doctor`](cli/doctor.md) reports `gcloud` missing or the login expired | The cloud is unavailable. A `gcp` pin would wait on every run; `prefer-gcp` is already placing here |
| Repeated `remote_host_interrupted` on one instance | Pre-emption pressure in that zone. The work is re-askable, but every attempt pays the bundle, the clone and a possible cold start again |
| `kew status` showing a queue that is not moving while every instance holds a run | A `gcp` pin is waiting by design. `prefer-gcp` is the policy that keeps the queue moving through a full cloud |

### What to measure

The question a day of placed runs answers is whether this machine got quieter
without the wall clock or the bill getting worse. Every number it takes is
already recorded somewhere:

| Measurement | Where it comes from |
| --- | --- |
| Operator-machine load, before and after | `uptime` on this machine, with a full fleet of runs placed here and again with the same fleet placed on gcp |
| Run wall clock, local vs gcp | [`kew export`](cli/export.md) — each run row carries `started_at`, `finished_at`, `duration_seconds`, `run_host` and `run_host_instance`, so the comparison is a group-by over the ledger rather than a stopwatch |
| VM cost per run and per day | The instance's running minutes × its spot rate, plus the disk's daily share. The arithmetic, and why the disk is the larger half of the bill for a host that runs an hour a day, is [What a host costs](#what-a-host-costs) |
| Credit left | The project's billing page. A credit is the one number the ledger cannot see |
| Pre-emptions, and how they recovered | The `remote_host_interrupted` interruptions: the category a pre-empted placement is reported under, and the `error_category` a run row recording one carries — and, for each, the run that followed it |

Record the numbers against the epic or the issue they were taken for, not in
this page: a measurement is dated, and this page is the contract.

## Quota

Two quotas bite, both per *region* rather than per project:

- **CPUs** (`CPUS` / `CPUS_ALL_REGIONS`) count only instances that are
  *running*. A set of hosts that are stopped most of the time can exceed your
  CPU quota in aggregate and still start one at a time.
- **Persistent disk** (`DISKS_TOTAL_GB`) counts whether the instance runs or
  not. A stopped host keeps its disk, and its disk bill.

Read what you have, and what you are using, before adding a host:

```bash
gcloud compute regions describe us-central1 --format='table(quotas:format="table(metric,limit,usage)")'
```

New projects start with small defaults, and a quota increase is a request with a
human on the other end — ask before the day you need the second host, not on it.

## Idle stop

A host stops itself. `release()` arms a transient `systemd` timer on the
instance — `systemd-run --on-active=Nm --unit=kew-idle-stop systemctl poweroff`,
where `N` is [`idle_stop_minutes`](configuration.md#hosts) (default 30) — so a
crashed driver cannot leave a VM running and billing. The next job's
`ensure_up()` cancels that pending stop and **proves** it was cancelled before
shipping anything, because a stop that fired mid-copy is exactly how a job dies
for a reason nobody can see. A `release()` that cannot claim the host is a
no-op: another process holds it, and that process owns arming its shutdown.

A timer and not `shutdown -h +N` because `shutdown` writes `/run/nologin` five
minutes before it fires, and pam then refuses every unprivileged login — the
exact login `ensure_up()` needs to cancel the stop. A timer waits without
closing anything. A `shutdown` armed by an older kew, or by hand, is still
cancelled; if its nologin window has already closed logins, kew waits the stop
out and starts the instance again, since no login can reach inside that window.
That wait is budgeted for the whole five-minute window **and** the stop that
follows it — about ten minutes — because the window is at its widest exactly
when kew arrives at the start of one. If the instance is still refusing logins
after kew has waited it out and started it again, kew says so and ships
nothing: someone else is stopping that host.

Values below `1` are refused — a zero delay is an immediate stop, not an idle
grace.

**The host holds itself, not just the operator.** The per-host lock is a file
lock on the operator machine, one directory per user —
`~/.local/state/kew/hosts/` (or `$XDG_STATE_HOME/kew/hosts/`, or
`$KEW_HOST_LOCK_DIR`) — named for the instance, so every kew process and
project addressing that instance contends for the same file. But a lock on the
operator machine dies with the operator process, and the job it shipped does
not. So every job also takes a **lease on the instance itself**:
`/run/kew-host.lease` names the kew process holding it and until when — the
job's own ceiling plus five minutes, or `job_timeout_minutes` before the job
starts. Another kew process — on this machine after a crash, or on another one —
that finds a live lease is refused with *still leased to another kew job* and
ships nothing, rather than deleting the checkout a job is still running in. A
release over another process's live lease arms nothing: that process owns the
shutdown. The lease is on `/run`, so a reboot, which ends every job, clears it.

The lease names the operator machine, the process's pid and the machine's
**boot id** (`/proc/sys/kernel/random/boot_id` on Linux, `kern.bootsessionuuid`
on macOS). A kew process that finds a lease held by its *own* machine checks the
holder: one taken in an earlier boot — the operator rebooted mid-verify — or
whose pid is no longer running has lapsed. An earlier boot means one kew recorded
for this machine, in a `boots` file beside its host locks: a second operator
machine with the same hostname names boots kew never recorded here, so its live
job is never mistaken for an orphan. kew then stops that holder's orphaned
job on the instance (its whole scope, recorded when the job started) and takes
the lease in the same locked command, before anything ships. kew records a
`host_lease_takeover_pending` audit event before it asks the instance for the
takeover, and settles it with a `host_lease_lapsed` event naming the dead holder
as soon as the instance answers, before anything ships — so it is on record even
when kew is killed or the machine reboots mid-job. This holds for `kew verify`,
the mutation report, a dispatch placed on the instance and `kew hosts
provision`; with no run store to write to, the takeover is refused before the
instance is asked, and a gcp verification or dispatch placement with no run
store is refused before it touches the instance at all. A run store that will not take a write — full,
read-only — is found before the instance is touched too: kew tries an audit
write and rolls it back first, and refuses the job when it fails. Every event carries one `takeover_id`, which the
instance also keeps beside the lease, and a `resource` naming the instance by
project, zone and name, so a same-named instance elsewhere never reads or
settles another's takeovers; each takeover is settled once. A takeover the
instance refuses is settled as `host_lease_takeover_refused`. So is one that
finds, under the instance's lease lock, that the lease no longer names the
holder kew saw lapse — it expired or was released in between — with
`lease_changed` set: nothing is stopped or marked, and kew reads the lease
again rather than taking over on what it saw. A takeover whose
command fails any other way — say the backstop would not arm, or ssh dropped —
is settled before the error is reported, by reading the instance's record back:
`host_lease_lapsed` when it names the takeover, otherwise
`host_lease_takeover_unconfirmed`. When that read fails too — the instance is
unreachable — the takeover stays pending rather than being settled on no
evidence, and the next hold settles it as below. A
pending takeover nobody is settling — its operator died, or its own settle
failed and the job retried — is settled by the next kew process on this machine
that leases the instance, before anything ships: as `host_lease_lapsed` when the
instance still names that takeover, and as `host_lease_takeover_unconfirmed`
when it does not (a stop clears it, and a kill before the instance was asked
leaves nothing to name), since whether that lease was taken is no longer known.
`confirmed_on_host` says which. A process of this machine that is still alive
but failed to write its answer counts as nobody: takeovers are only asked for
under the host's exclusive claim, so the next process to hold that claim
settles them. Whichever process settles a takeover by reading the instance's
record also fences that takeover in `/run/kew-host.lease.fenced` while it holds
the same lease lock, and settles it only once that locked command succeeded. A takeover command that reaches the instance later, such
as a `gcloud` session that outlived its killed operator, is refused
and takes nothing, so `host_lease_takeover_unconfirmed` stays true. A takeover whose event cannot be written fails
the step that took it over, and no job runs under it. The takeover goes ahead only
once that job is proved gone: a lease whose holder left no scope record, or whose
scope still has a process in it after `SIGKILL`, is refused until its deadline.
A job whose scope cannot be recorded and started is not started. A holder that
is still running, or one on another machine, is refused until its deadline as
before. `kew verify` releases its lease on `SIGTERM` as well as `Ctrl-C`, and
`kew hosts release gcp --holder TOKEN` drops a named lease by hand
([kew hosts](cli/hosts.md#kew-hosts-release)).

Each job runs in a **scope** of its own: before the job starts, its shell is
moved into a transient systemd scope, `kew-job-<nonce>.scope`, a cgroup every
process the job starts inherits. The scope is recorded, under the job's lease
token and with the time the job started, in `/run/kew-job.scopes`, written
through `sudo`: the file is root's, and the login cannot write it directly.
The record and the scope's start happen under the instance's lease lock, so a
stop never mistakes a scope recorded but not yet started for one that has
ended: it waits for the start, then kills the scope. Under that lock the job
first checks that the lease still names it: if a release or a takeover took
the lock first and removed the lease, the job records nothing and never
starts. A stop needs a record naming the holder, and then runs `systemctl kill
--signal=KILL` on every `kew-job-*` scope on the instance — not only the ones
the record names — confirmed only once each scope reads inactive, or systemd
reports it no longer loaded (a finished scope is collected); a scope whose state
cannot be read keeps the lease. A process the
job detached with `setsid` before it ended cleanly left the job's session but
not its scope, and still holds the lease until it is dead. A container the job
started with `docker run -d` is the docker daemon's, not in the job's scope, so
the stop then runs `docker rm -f` on every container created or started since
the holder's recorded start that is still running or only created — a `docker
run` not yet through — and keeps the lease while `docker ps -a` still lists one
of them, cannot list them, or lists one `docker inspect` cannot say the times
of. The containers count as gone only after two listings in a row, half a
second apart, show none of them, since a container the daemon has just accepted
is listed only a moment later. An instance with no `docker` client cannot list
them at all, so a holder with a recorded job keeps its lease there, whether
or not a daemon seems to be running: a rootless daemon, or one on a socket of
its own, can't be ruled out. The instance needs the `docker` client for a
lease that ran a job to be released without `--force`; a lease that only
held the instance — a first provisioning that failed before Docker was
installed — started nothing, and is released as usual. A container created and running before
that start is left alone, and so is one that has exited. Taking a lease records its start too, so the containers a
placed run started under it are found the same way. The ssh login needs
passwordless `sudo` and the instance needs systemd's `busctl`.

A lease past its deadline is not proof its job ended. The first lease command
reports an expired foreign holder without changing the host. kew then records
`host_lease_takeover_pending` before it asks for the same marked takeover used
for a lapsed holder. Exactly one terminal event is recorded before anything
ships. The stop runs on every job scope and on the containers started since the
expired holder's recorded start. The lease is kept while any process or
container survives the kill. An expired lease no scope record names with a
start time is kept too. Nothing then identifies its job or containers. The
refusal names `kew hosts release NAME --holder TOKEN --force`, which drops it.

Every command that removes a lease takes this one path: a takeover, a release,
the drop of a placed run's lease, and `kew hosts release` without `--force`.
Under the lease lock it matches the holder exactly, whether or not its lease
has expired, stops that holder's scopes and containers and confirms them gone,
and only then removes the lease. A release or a drop that finds another
holder's expired lease leaves it, and its job, alone: only a takeover stops
another holder's work. `kew hosts release --force` is the one way to drop a
lease without the stop, and it says so.

That `sudo` is also the job's: a job runs as the ssh login, so candidate code
can rewrite the record or move itself out of its scope as root. A rewritten
record cannot steer a stop away from the job, since the stop does not take the
record's word for which scopes to kill, and a record with the holder's line
removed only keeps the lease until an operator drops it. A job that uses `sudo` to leave
its scope is not contained — the scope stops what a job runs without root, and
an instance is a verification host, not a sandbox.

Every release stops the job its own process ran on the instance first, in the
same locked command: an interrupted operator ends only its side of the ssh
session, and a job process that ignores the hangup would otherwise run on in a
checkout the next job deletes. If that job cannot be proved stopped — no scope
record names it, or a process is left in its scope after `SIGKILL` — the
release keeps the lease and the backstop below and the instance is freed at
the lease's deadline instead. `kew verify` reports it as the result's `error`
(*not proved stopped*), in its output and its `verify_completed` event, beside
the gate's verdict, which it does not change.

Taking the lease also arms a **backstop** power-off on the instance, a second
timer (`kew-host-deadline`) set for the lease's deadline plus
`idle_stop_minutes`. `release()` replaces it with the ordinary idle stop. An
operator killed mid-job never reaches `release()`, so without the backstop the
instance it had disarmed would run, and bill, until someone noticed.

A placed run that could not arm its idle stop when it let the instance go keeps
its verdict, and says so in the run's own log: *could not arm the idle stop on
gcp instance … it may keep running and billing*.

**A VM with an in-flight run on it is never stopped.** Before a placement arms
an idle stop, kew asks the ledger whether any *other* run is still in flight on
that instance, and skips arming the stop when one is. The ledger, not the lock
the finishing run is holding: a placement is recorded **before** its instance is
claimed, so another run can be attached to that machine — queued for it, or
mid-placement from another process — while this one finishes, and arming the stop
would take the machine down under it. Skipping a stop costs idle minutes; taking
one wrongly costs the run. The finishing run's own record is excluded from that
question, so a lone run still arms its own stop and no VM is left billing.

That question is age-bounded, at twice `[agent] timeout_minutes`. Nothing on the
operator machine can prove a container on a VM dead — its process table and its
container daemon are both the wrong machine's — so a run killed mid-flight leaves
a record that would otherwise hold its instance up for good, with no later
release ever arming a stop. A record older than the bound no longer describes
anything running, and stops protecting the machine.

An instance also reads `RUNNING` before it accepts logins, so the first ssh
after a cold start is refused by the *connection*, not by the host.
`ensure_up()` retries it for about a minute before reporting the instance
unreachable.

## When a session dies

The failure a host lock cannot survive is a connection that dies **silently**.
A half-open TCP connection leaves the local `ssh` waiting on a peer that will
never speak again: the instance has nothing left of the session — no `sshd`, no
job, load 0.00 — while this end holds the session, and the host lock, open. One
such session held a verification open for 66 minutes.

Three things bound it, and all of them are on by default:

- **Keepalives.** Every SSH and SCP session kew opens to a host carries
  `ServerAliveInterval=15` and `ServerAliveCountMax=4`, so four unanswered
  probes end a dead connection in about a minute.
- **A kill that ends the session.** Every ceiling — a job's own, a recipe's, the
  deadline below — kills the session's whole process *group*, because
  `gcloud compute ssh` is a wrapper and the `ssh` it spawns is what carries the
  job: ending only the pid kew holds leaves that child connected, with the job
  still running on an instance kew has just released and armed the idle stop on.
  Any other escape — a `Ctrl-C` at the terminal, a signal, an error raised above
  the session — ends it the same way.
- **A job deadline.** A job that names *no* ceiling —
  `verify.timeout_seconds = 0`, unbounded everywhere else — runs under
  [`job_timeout_minutes`](configuration.md#hosts) (default 240) instead, because
  an unbounded wait on a session that will never end holds the host lock for
  ever. A ceiling the caller did name is honoured as asked: the deadline never
  cuts it down, which would end a merely slow job and report it as a lost
  connection.

Either way the job is reported as an **interruption**, never as a failed gate:
`host_job_deadline_exceeded` for the deadline, and `host_connection_lost` for a
session that ends without the exit marker kew reads a job's outcome from. The
distinction is the point — `gcloud`'s own exit status cannot tell a failed job
from a failed connection, and an interruption says nothing about the code under
test, so the work it asks for is the job again.

## What a host costs

The arithmetic matters more than the rates, which change: **the disk bills
whether the host runs or not, and for a host that runs an hour a day the disk
is the larger half of the bill.**

Worked example, for the `e2-standard-4` host created above with a 100 GB
`pd-balanced` boot disk in `us-central1`, at published list prices at the time
of writing — treat them as the shape of the bill, not as a quote, and check
[Compute Engine pricing](https://cloud.google.com/compute/all-pricing) for
today's numbers:

| Line | Rate | 1 h/day | 4 h/day |
| --- | --- | --- | --- |
| `e2-standard-4` while running | ~$0.13 / hour | ~$4 / month | ~$16 / month |
| 100 GB `pd-balanced`, always | ~$0.10 / GB-month | ~$10 / month | ~$10 / month |
| **Total** | | **~$14 / month** | **~$26 / month** |

Three consequences worth planning around:

- **Stopping is most of the saving, and it is automatic.** The idle stop is what
  turns a ~$105/month always-on VM into the rows above. Do not raise
  `idle_stop_minutes` to avoid cold starts without pricing the change.
- **A smaller disk is a real saving; a smaller machine often is not.** The disk
  is billed for all 720 hours of the month, the CPU only for the hours a job
  runs.
- **A sharded set of hosts multiplies the disk line, not the CPU line.** Four
  hosts sharing the same total work cost roughly four disks and one machine's
  running hours.
