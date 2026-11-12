# Container entrypoint contract

**Status:** normative. This page is the canonical text; `kew/src/kew/sandbox.py`,
`kew/src/kew/dispatch_sandbox.py` and `docker/agent-entrypoint.sh` point *here*
rather than restating the contract.

When `[sandbox] enabled = true`, kew does not run a coding agent as a host
subprocess — it runs a **container image** and talks to it across a narrow,
fixed interface. This page specifies that interface.

The organising invariant:

> **Per-runner variation lives inside the image. kew's host-side contract never
> changes.**

kew builds the same `docker run` argv, hands over the prompt the same way, and
reads the same two channels back, whether the image wraps `claude`, `opencode`,
or something that does not exist yet. The five elements below are what an image
must satisfy to be *containable by kew* — there is no per-image special-casing
on the host side, and there will not be one.

Why this matters: the sandbox path grew up claude-only, and the coupling was
live-proven expensive — a contained dispatch routed to a non-claude runner
handed an OpenRouter model string to the `claude` CLI inside the kew-agent image
and 404'd in 1.3s (#779). The fix is a contract, not a second code path.

## What is built today

This page is normative for **image implementers now**: elements 1, 2 and 5 are
fully enforced by the shipped host code, and an image that violates them fails
today. The remaining host-side work is the containment epic, and until it lands
the host half is still claude-shaped in one specific place:

| Gap | Today | Closed by |
|---|---|---|
| Image selection | `[sandbox] image` is a **single string** (`config.py`), so every contained dispatch gets the same image regardless of runner — routing a non-claude node into a sandbox hands it the claude image, which is the #1104 dead combo above. | N2 (`[sandbox] images.<runner>` map, fail-closed on an unmapped runner) |
| ~~Extra args (element 3)~~ | **Closed by N3.** `run_container_and_stream` now fills the slot from `runner.container_args()` — the resolved runner's own flags, not a claude block. | — |
| ~~Stdout parsing (element 4)~~ | **Closed by N4.** The sandbox tail loop and the terminal telemetry replay both go through `runner.parse_line()` — the same parser the host path uses — so a non-claude image's tokens and native cost reach the run row, and the row records the runner that actually ran. | — |

Until N2 lands, **do not route a non-claude node through the sandbox** — the
epic deliberately runs every one of its own nodes on the claude route for
exactly this reason.

---

## The five elements

### 1. Mounted checkout

kew clones the target repository into a temporary standalone checkout on the
host and bind-mounts **that directory and nothing else**:

```bash
-v <checkout_dir>:/work   -w /work
```

- The mount point inside the container is `/work`, and it is also the working
  directory. Both are fixed.
- It is the container's **only** bind mount. Host `~/.claude`, `dispatch.db`,
  GitHub credentials, and the rest of the host filesystem are outside the mount
  and invisible. This mount boundary is the primary containment — the
  in-process security guard is best-effort layer 2 (see
  [Security](security.md), [Threat model](threat-model.md)).
- The image **MUST NOT** assume any other host path exists.
- Everything the agent produces in files that kew will act on — commits and
  the prompt file — lives under `/work`. A reviewer's verdict is not a file:
  it travels on stdout (element 5).

The container never pushes and never opens a PR: it holds no GitHub token by
design. After a clean exit the **host** brokers `git push` and `gh pr create`.

### 2. Prompt file → stdin

The prompt is **never** passed in argv (no length limit, not visible in `ps`)
and is **never** piped from the host process.

1. Before launch, the host writes the prompt to `<checkout_dir>/.kew-prompt.txt`
   — `PROMPT_FILENAME` in `kew.sandbox`.
2. Because the checkout is mounted at `/work`, the file appears inside the
   container at `/work/.kew-prompt.txt` — `PROMPT_MOUNT_PATH`.
3. The entrypoint **MUST** read that file and feed it to the agent binary on
   **stdin**.
4. The host deletes the file on every exit path — success, failure, or kill
   (#725) — so it can never land in a PR branch.

The entrypoint **MUST** honour `KEW_PROMPT_FILE` as an override, defaulting to
`/work/.kew-prompt.txt`, and **MUST** fail loudly (non-zero exit, message on
stderr) when the file is missing or empty rather than starting the agent with
no prompt. That failure mode is exactly #695: the container started, the agent
exited on "Input must be provided either through stdin or as a prompt
argument", and the dispatch burned for nothing.

### 3. `"$@"` — the extra-args slot

Everything after the image name in the `docker run` argv becomes the container
**command**, which the entrypoint receives as `"$@"`:

```bash
docker run [hardening flags] -v <checkout>:/work -w /work \
    -e AGENT_ASSIGNED_WORKTREE=/work \
    -e ANTHROPIC_API_KEY -e CLAUDE_CODE_OAUTH_TOKEN \
    <image> <extra args ...>
```

- The entrypoint **MUST** forward `"$@"` to the agent binary unmodified, and
  **MUST NOT** hardcode flags that the host is responsible for.
- This is the slot the routed **model** travels through. Routing that only
  reaches the run row and not the container is the #761 bug class.
- This slot is where per-runner difference shows up on the host side. The host
  fills it from the resolved runner's `container_args()` — `build_command()`
  minus the binary and minus the prompt handoff. For `claude-code` that is
  `--dangerously-skip-permissions`, `--verbose --output-format stream-json`,
  and `--model <routed model>`; for another runner it is that runner's own
  flags. An image therefore receives args shaped like the agent it ships, and
  the image-side obligation (forward `"$@"` unmodified, hardcode nothing) is
  the same either way.
- A runner deliberately omits **host-only** argv from this slot: a session id
  minted for a host-side resume, or a sidecar path under the host log dir,
  means nothing inside the container. The prompt is omitted for the same
  reason it never rides in argv at all — element 2 already delivers it.

### 4. One JSON event per line on stdout

The agent's telemetry channel is **stdout, one JSON object per line** —
newline-delimited JSON, no pretty-printing, no multi-line objects, no
interleaved prose in agent telemetry.
The wrapper acknowledgement described below precedes that telemetry.
Kew consumes the acknowledgement before parsing telemetry (tokens, cost, reported model, session id).

- After its acknowledgement, the image **MUST** emit newline-delimited JSON on stdout.
- Human-readable diagnostics belong on **stderr**.
- A line kew cannot parse **MUST NOT** be fatal to the image — kew tolerates
  unparseable lines (they still reach the log), but telemetry derived from them
  is lost. An image that emits prose on stdout is cost-blind, not broken, which
  is the worse failure: it looks like it worked.

For the reference image this is `claude --verbose --output-format stream-json`.
An image wrapping a different agent is responsible for producing the same
*shape* of channel: the "one JSON object per line" framing is the fixed part.

The event **vocabulary** inside those lines is the resolved runner's own. kew
feeds every line to that runner's `parse_line()` — the same parser the host
(non-sandbox) path uses — so an image speaking opencode's vocabulary is metered
as accurately as one speaking claude's. Concretely, telemetry is derived from
the parsed `StreamEvent`s: `usage` events accumulate tokens, `step` events sum
the runner's native per-step cost, `metadata["model"]` names the model, and a
terminal `result` event that carries a `RunTotals`
(`kew/src/kew/runners/protocol.py`) overwrites the accumulation with the run's
authoritative totals. A runner that reports no end-of-run summary simply omits
it and its per-step sums stand.

An image whose vocabulary matches **no** kew runner still runs and still logs —
it is cost-blind, not broken (the #761 failure class). The fix for such an image
is a runner implementing `parse_line()` for its shape, not a change here.

#### Agent startup evidence

The host sets `KEW_AGENT_START_NONCE` to a fresh 32-character hexadecimal nonce for each attempt.
This value is an attempt identifier, not a credential.
Each reference entrypoint emits exactly one stdout acknowledgement immediately before executing its agent:

```text
KEW_AGENT_START <KEW_AGENT_START_NONCE>\n
```

The newline terminates the line; it is not literal backslash text.
The host accepts only an exact nonce match for the current attempt.
The acknowledgement is not an agent event and never enters the events file.
The host filters acknowledgement lines before runner parsing, including mismatched acknowledgements.
It filters only a whole acknowledgement line: the prefix and one nonce-shaped token, with nothing after them, so text glued after the prefix still reaches the stream readers.
Generic JSON lines never establish startup, including `{"type":"bootstrap","ready":false}`.
Container `Running`, `StartedAt`, and `start` events prove wrapper startup only.
The acknowledgement is the only startup evidence: every contained agent is exec'd through the entrypoint, which emits it.
No daemon event confirms agent startup, including an `exec_start` of the entrypoint or of an agent binary.
An image that runs an agent without emitting the acknowledgement has every completed run classified `sandbox_startup_refused`.
Each CPU retry receives a fresh nonce and event window before spawning.

A completed wrapper without the current attempt's acknowledgement is `sandbox_startup_refused`.
This includes a wrapper exiting `0`; publication is forbidden and public exit is `2`.
Refusal diagnostics remain in the failure record.
Entrypoint exit `64` means the prompt is missing; entrypoint exit `78` means a prerequisite is unavailable.
Before acknowledging startup, each entrypoint resolves its agent with `command -v`.
The resolved path must be an executable regular file (`test -f` and `test -x`).
Failure prints a static diagnostic and exits `78` without an acknowledgement.
A stream failure before acknowledgement makes execution `UNKNOWN`, unless an explicit entrypoint refusal confirms no launch.
`UNKNOWN` retains the failure record and work, forbids refusal rollback, and returns public exit `1`.
Its reason names the missing acknowledgement and stream error.
Confirmed startup followed by failure returns public exit `1`, including agent exits `64` and `78`.
After the stream completes, acknowledgement without an agent event and exit `126` or `127` means exec failure.
The host classifies this attempt as `sandbox_startup_refused`, with `launched=False`.
Without earlier execution, public exit is `2` and refusal rollback is permitted.
An agent event after acknowledgement preserves execution, including exits `126` and `127`.
All other post-acknowledgement exits preserve execution.


### 5. Reviewer verdict channel

A reviewer dispatch runs through the same primitive
(`run_container_and_stream`) but must return a **verdict**, not a branch. The
verdict travels in the reviewer's **own final result event on stdout** (#2021),
never in a file:

- The reviewer ends its run with one final message carrying its verdict JSON
  between two delimiter lines, `<<<KEW-REVIEW-VERDICT-BEGIN>>>` and
  `<<<KEW-REVIEW-VERDICT-END>>>` (`kew.review_stream.VERDICT_BEGIN` /
  `VERDICT_END`). The host appends these instructions, with the recorded head
  SHA, to the pinned reviewer prompt.
- The host reads it only from the runner's **typed terminal record** as it
  streams the container's stdout: for `claude-code`, the top-level
  `{"type": "result"}` event. Assistant turns, tool results and free text are
  never read as a verdict.
- The stream must carry **exactly one** result event and it must be the
  **last** line: any later non-blank line, parseable or not, and any line the
  host reader split, fails the round. The result must report success; its text
  must carry exactly one delimited block; the block's `head_sha` must equal the
  SHA the host recorded; and the container must exit successfully. Anything
  else fails the round.
- Only `claude-code` has such a record today. `[review] runner` set to any
  other runner is a configuration error at load, and the launcher refuses one
  again before any container or credential access.

Why not a file: the reviewer executes the PR's own tests as acceptance
evidence, in the same container, as the same user, over the same `/work`
mount. Any file there, at any depth, can be written by the code under review
with the right head SHA. kew therefore reads no verdict file at all.

- The image **MUST** leave the runner's stdout unmodified: no wrapper may
  filter, reorder or synthesize result events.
- kew stores the verdict block it extracted on the host and digests **its own
  stored copy** (F10). Nothing in the image can influence that digest.
- A missing, ambiguous or unparseable verdict is **fail-closed**: the node is
  held for a human, never auto-approved.

### 6. Extraction artifact

> **Status: active for canonical publication.** The synchronous host-owned
> publication path uses this artifact for contained dispatches. Native runs use
> an equivalent host walker. `publication_job.PUBLICATION_JOBS_ACTIVE` remains
> `False` because the unattended job lifecycle stays dormant. Shadow mode also
> uses this contract for comparison evidence.

The host must never point git at a directory an agent can write. Reading a
repository *runs code from it* — `core.fsmonitor` and `filter.*.clean` execute,
and a repo-local `.git/config` cannot be suppressed (there is no
`GIT_CONFIG_NOLOCAL`). So extraction is specified to happen **inside the
container**, where untrusted code already runs and this grants an attacker
nothing new, with the host reading the result as opaque bytes.

`docker/extract-artifact.sh` is the reference producer:

```text
extract-artifact.sh <base-commit> <output-dir>

  <output-dir>/manifest.json   {"base", "entries": [{path, mode, sha256}], "deletions": [...]}
  <output-dir>/blobs/<sha256>  content, addressed by its own hash
```

- **Content is addressed by digest, never by path.** The manifest's paths are
  attacker-controlled; if content were laid out at those paths, merely
  *unpacking* the artifact would already have written wherever the agent chose,
  before any filter had an opinion. Addressing by hash keeps the path pure data
  until it is approved.
- **Deletions are carried explicitly.** A format that cannot express a removal
  silently leaves every deleted file in place, so the publication would differ
  from the work it claims to carry. Renames are extracted as a delete plus an
  add (`--no-renames`), because a rename record asks the host to trust a pairing
  it cannot verify.
- The producer **MUST NOT** carry anything that is not a regular file at
  `100644` or `100755`. It drops them rather than refusing the whole
  publication; this is belt, not braces — the host refuses those modes as well.
- The image **MUST** provide `git`, `jq` and `sha256sum`. The reference producer
  is POSIX-ish bash for exactly this reason: the kew-agent image has no
  `python3`, and `jq` is what makes path escaping correct for the one input that
  is entirely attacker-controlled.
- Nothing in the image is trusted to produce an honest artifact. The host
  re-hashes every blob, refuses unreferenced content, and re-verifies on every
  read — so tampering after extraction is caught, not assumed away.

#### How the host runs it

The image **MUST** install the producer at `/usr/local/bin/kew-extract-artifact`,
executable. It is *not* the image ENTRYPOINT: extraction runs as a **second
container** over the finished checkout, after the agent has exited. Today that
second container is launched only by shadow mode; when the broker activates, it
becomes the publication path itself.

That separation is deliberate. It is additive, so a failure here cannot damage a
run that already succeeded; it is identical whichever runner produced the work,
so publication does not quietly depend on which image was chosen; and nothing is
racing the tree it reads.

`build_extract_argv()` in `kew/src/kew/sandbox.py` is the host half. Every way
this container differs from the agent's is a **reduction**:

| Flag | Why it differs from the agent's container |
|---|---|
| `--network none` | The agent needs egress; extraction reads committed objects on a local disk and needs none at all. |
| *(no runner credential env)* | Extraction never receives runner credentials. `HOME=/tmp` sets its temporary home. Proxy credentials follow `proxy_environment`. |
| `-v <checkout>:/work:ro` | Extraction reads. `/artifact` — the host-created output directory, mounted writable — is the only thing it may write. |
| `--name kew-extract-<slug>` | A collision with `kew-<slug>` would make `docker kill` ambiguous about which of the two containers it stops. |
| `--user <host uid>:<host gid>` | The artifact is written for the host to read. Writing it as anyone else means either a directory the host cannot read, or one loosened until it can — and matching uid also settles git's dubious-ownership check on the mounted checkout. |

Extraction controls these ten proxy variables:

<!-- extraction-proxy-variables:start -->
`HTTP_PROXY`, `http_proxy`, `HTTPS_PROXY`, `https_proxy`, `FTP_PROXY`, `ftp_proxy`, `ALL_PROXY`, `all_proxy`, `NO_PROXY`, `no_proxy`.
<!-- extraction-proxy-variables:end -->

Under `proxy_environment = "none"`, each variable receives an empty assignment through `-e NAME=`.
Under `proxy_environment = "inherit"`, present HTTP, HTTPS, and NO host variables use `-e NAME`.
ALL and FTP variables always receive `-e NAME=`, including lowercase names.
Absent variables receive `-e NAME=` under both policies.
Inherited values can contain proxy credentials, including `HTTP_PROXY=http://user:secret@proxy`.
Proxy values never enter argv.
Runner credentials never enter the extraction environment under either policy.
Extraction retains `--network none` under both policies.

Everything else — `--rm`, `--cap-drop ALL`, `no-new-privileges`, `--read-only`,
the sized `--tmpfs /tmp`, and every resource cap in the table below — is
**identical** to the agent's container, and a test asserts the two do not drift.
A cap present on only one of the two containers is a cap an attacker avoids by
choosing the other one.

The base commit is passed as a **full hex object id**, resolved from the
canonical repository. Not from the agent's checkout: asking the agent's own repo
what its base is means accepting its answer. A value that is not a resolved oid
is refused before launch, because the producer takes it as `$1` and anything
beginning with `-` becomes an option to the git commands it runs.

---

## Host-side hardening kew applies

`build_run_argv()` in `kew/src/kew/sandbox.py` is the host half of the contract. An
image implementer does not choose these — they are the environment the image
must survive:

| Flag | Effect on the image |
|---|---|
| `--read-only` | The root filesystem is **read-only**. Nothing may be written outside the mount and the tmpfs mounts below. |
| `--tmpfs /tmp:size=1g` | Ephemeral scratch, mounted `noexec`, and **sized** — a tmpfs is host RAM. Sized `min(1g, max_memory / 4)`, so a lowered memory cap pulls it down. |
| `--tmpfs /home/agent:exec,size=4g,uid=1001,gid=1001,mode=0700` | A writable, **ephemeral HOME**, pinned to the image's agent uid, sized, and deliberately `exec` (#762) so tooling the agent installs is runnable there instead of littering the bind-mounted checkout. Sized `min(4g, max_memory / 2)`. |
| `--cap-drop ALL`, `--security-opt no-new-privileges` | No capabilities, no privilege escalation. |
| `--pids-limit 1024` | Fork-bomb bound. A hard cap: no config key, environment variable or flag changes it. Docker and Podman only — an `openshell` sandbox gets no docker argv, and its agent prompt does not claim the cap. Docker counts **threads** toward it, so a browser-driven gate such as `npm run ui-check` uses a large share — stop any dev server you started before running it (ui-check starts its own). A sandboxed prompt names only the scoped form, `npm run ui-check -- <module> ...`; the merge verify runs every module. |
| `--memory 8g`, `--memory-swap 8g` | Hard memory cap, with swap **off**: equal values are how Docker spells "no swap". A swap allowance lets a memory bomb survive the cgroup limit and starve the host on IO instead. `[sandbox] max_memory` may **lower** the cap (`1g`–`8g`), never raise or remove it; swap follows it. |
| `--cpus N` (1–4) | The image gets at most four CPUs, or fewer if `[sandbox] max_cpus` lowered the ceiling. Kew clamps the value to the daemon's reported count. Kew asks once before launch. If capacity changes, kew confirms Docker created no container. Kew then re-probes and retries once at one CPU. An unavailable probe also uses one CPU. The agent container gets the same number as `PYTEST_XDIST_AUTO_NUM_WORKERS`, `VITEST_MAX_WORKERS`, `VITEST_MAX_THREADS` and `VITEST_MAX_FORKS`, so test pools fit the cap rather than the host. |
| `--ulimit nofile=4096:8192` | File-descriptor bound. |
| `--ulimit fsize=1073741824` | **No single file** may exceed 1 GiB — one `dd` cannot fill the bind-mounted checkout's filesystem. |
| `--ulimit core=0` | No core dumps into the mounted checkout. |
| `--rm`, `--name <launch-slug>`, `--cidfile <unique-host-path>` | One named container runs per launch and is removed on exit. The cidfile records its immutable ID. It also proves whether Docker created the container. |
| `--network` | `allowlist` joins `kew-allowlist` and fails closed when absent. `open` explicitly enables full egress and warns when a credential is injected. |

`--ulimit nproc` is deliberately **not** set: it is a per-UID *host* limit and
every agent container runs as the same uid 1001, so one container's bound would
apply across all concurrent ones. `--pids-limit` is the per-container fork bound.

Three caps have no Docker flag at all — a bind mount has no quota, stdout has no
size, and `docker run` has no deadline — so kew enforces them host-side while it
streams the container's output, and a breach issues `docker kill <name>`:

| Cap | Value | What it closes |
|---|---|---|
| Checkout quota | 4 GiB | A fill-disk agent writing many files under the 1 GiB per-file rlimit. |
| Checkout entry quota | 500k entries | An inode bomb: empty files measure 0 bytes against the byte quota. |
| Host free-space floor | 1 GiB | A fill-disk agent outrunning the quota walk. The walk backs off up to 20 s on a large tree; `statvfs` is O(1) and runs every interval. |
| Per-line read bound | 1 MiB | One newline-free write becoming unbounded **host** memory before any parser sees it. |
| Transcript byte cap | 64 MiB | A huge-log agent aimed at the operator's log directory. Enforced on the `execute_dispatch` stream today; the shared orchestrator path bounds each line to 1 MiB but has no total (see [security.md](security.md#host-side-resource-cap-coverage)). |
| Wall clock | 4 hours | A run that never ends. Independent of `agent.timeout_minutes`, which the orchestrator applies. |

A kill from these caps ends the stream with a named `kew_resource_cap` event.
The event prevents a capped run from looking silent.
Both contained launch paths use the same hard-stop classifier.
The implementation and reviewer paths both persist the same container evidence.
Both paths emit the same breaker audit event for a hard stop.
They audit `wall_clock` for a deadline and `resource_cap` for another confirmed cap.
They audit `hard_kill` for an unattributed 137 before the deadline.
That result can mean an in-container SIGKILL.
It can also mean an absent, unavailable, or clock-skewed OOM event.
A kew-ordered stop for pause, budget, or cost anomaly has no hard-stop classification.

A **cgroup OOM** is separated from those 137s before that classification. The
kernel's kill exits 137 like every other hard stop, and `--rm` removes the
container before `docker inspect` could read `State.OOMKilled`, so kew asks the
daemon's event stream, which outlives the container.
The query filters by this launch's container ID from `--cidfile`.
A missing, empty, unreadable, or invalid cidfile makes attribution unavailable.
The probe never uses the reusable container name as OOM evidence.
The event window applies clock slack before launch and after probe time.
The future `--until` bound makes an absent-OOM probe wait about five seconds.
A 12-second command timeout bounds that wait.
Three default attempts can therefore add about fifteen seconds.
Hard-stop classification starts when timeout enforcement begins.
Managed transport startup and workspace preflight occur before this boundary.
The OOM probe wait occurs after this boundary ends.
Neither setup nor probe time affects hard-stop classification.

A confirmed OOM becomes `memory cap (8g) hit: OOM-killed`, naming the cap read
back off the launch argv — so a container launched under a lowered
`max_memory` reports that value rather than the ceiling.
It also creates the normal `kew_resource_cap` evidence and breaker event.
An accepted watchdog kill remains the selected cause.
A concurrent OOM can have killed only a child process.
When no watchdog kill was accepted, a confirmed OOM becomes the selected cause.
The evidence retains both observations.
The source retains at most eight observations.
It keeps the first observation and the newest observations.
The sidecar and audit payload record `resource_cap_observations_truncated`.

The probe records `seen`, `absent`, or `unavailable`.
An unavailable probe never invents an OOM.
Its bounded diagnostic persists in the evidence sidecar and audit payload.

Stable cap codes control retry policy.
Checkout byte and entry caps are terminal because retries reuse the same tree.
A confirmed OOM is classified `resource`, like any other 134 or 137 exit that
no deadline or intentional stop explains: the run records the signal and the
runner's last non-protocol output lines, and it is retried once per commit,
and at most twice per epic node across commits. The next such failure holds
the node; `kew epic unhold`, then `kew epic retry`, relaunches it.
Host free-space and transcript caps remain retriable because their state can change.
An accepted retriable watchdog cause stays retriable despite a competing OOM.

Credentials are forwarded **by name only** — `-e <NAME>`, never `-e
NAME=value` — with values inherited from the host process env; an unset name
stays unset inside. CLI login state can never reach a container, because
`~/.claude` is deliberately not mounted, so an env credential is the only auth
path in (#721). No GitHub token is ever passed.

**Which names are forwarded is keyed by the resolved runner** (#1146). Each
runner declares its own `required_credentials`, and `build_run_argv` injects
exactly that runner's set and nothing else, so a contained run only ever sees
its own provider's credentials:

| Runner | Forwarded names |
|---|---|
| `claude-code` | `-e ANTHROPIC_API_KEY -e CLAUDE_CODE_OAUTH_TOKEN` |
| `opencode`, `pi`, `hermes` | `-e OPENROUTER_API_KEY` |
| `codex` | *(none — the declaration is empty on every runtime. On the docker route that refuses containment; on `openshell` the dispatch IS contained and the gateway injects at the endpoint instead of forwarding a name)* |
| `lemonade` | *(none — declares no in-container credential, so it is refused rather than contained)* |

Contained direct OpenRouter runs require host `OPENROUTER_PROVISIONING_KEY` for bounded issuance.
Missing provisioning refuses launch with `OpenRouter provisioning key required for direct routes` and exit 2.
Every container environment removes the ambient `OPENROUTER_API_KEY` before explicit bounded-key injection.
The container still receives only `-e OPENROUTER_API_KEY` by name.
Its launch environment supplies the issued value without changing the host environment.
The provisioning key never enters the container environment or argv.
The route profile records the key hash, dollar limit, and UTC expiry.
Foreground runs revoke the key at run end.
The epic driver mints detached keys only for direct OpenRouter routes before spawn.
Driver ticks and delegation watcher passes revoke terminal or expired keys.
Both parents reconcile again on exit while preserving running children.
Generic runtime helpers receive neither an issued key nor its marker.
Children never receive provisioning authority; expiry bounds revocation failures.
See [bounded credentials](cli/sandbox.md#bounded-openrouter-credentials) for budget and expiry rules.

The host sets two non-credential variables for every runner.
`KEW_AGENT_START_NONCE` binds the startup acknowledgement to its attempt.
The existing checkout marker remains:
`-e AGENT_ASSIGNED_WORKTREE=/work`. It is the security guard's dispatched-agent
marker, given as the container path of the mount, so the guard's agent-only
rules (the `.claude/` Write/Edit block and the cross-checkout ref rule) apply
inside the container exactly as they do on the host (#1691). It carries a
value because a container never inherits the host child env; it is a path,
not a secret.

A claude container therefore never carries `OPENROUTER_API_KEY`, and an
opencode container never carries `CLAUDE_CODE_OAUTH_TOKEN`. This is a
**security surface**, not a convenience list: an opencode image that could read
the claude token could spend the operator's Anthropic subscription. Widening a
runner's declaration widens what a prompt-injected agent in that container can
exfiltrate — and it must never widen *how* credentials travel (still by name,
still no mounted credential file, still no host path).

The Codex image ships no entrypoint of its own. It is OpenShell-only, and
`openshell sandbox exec` replaces the image `ENTRYPOINT`, so the prompt handoff
and acknowledgement an entrypoint would perform live in
`docker/kew-openshell-launch` instead — see "When the runtime replaces the
entrypoint" below. That is a containment runtime, not a docker-route entrypoint:
it does not enable external Codex containment on the docker route.

Official Codex is deliberately outside this **docker-route** container contract
in v1, which is a per-runtime statement and not a flat one. On
`runtime = "docker"` or `"podman"` it uses Codex-native `workspace-write` on the
host with the official CLI login, and mapping an image does not change that: the
runner declares `native` on those two runtimes, so no container is launched for
it there. `openshell` is the exception, and it is a real one.

On `runtime = "openshell"` a mapped `codex` image **is** contained, and the
empty credential declaration is not what would stop it — the by-name
forwarding rule this section describes does not govern that lane at all. The
gateway holds the
credential in the provider named at `[sandbox.openshell] providers.codex` and
injects it at the endpoint its policy governs; the sandbox sees placeholders,
and the `$HOME/.codex/auth.json` the in-sandbox launcher writes is one of them
(see "When the runtime replaces the entrypoint" below and
[the Codex reference](runners/codex.md#posture-per-runtime) for the runs on that
runtime which still stay on the host). What does gate it there is the same
admission every other contained dispatch passes: a mapped image, a present
gateway provider, and a fresh egress certification.

No personal `.codex` directory or login cache is mounted into a container on
either lane.

Consequences an image **MUST** be built for:

- `HOME` is fresh every run: no `~/.gitconfig`, no cached agent config, no
  credential store. Anything the image needs at `HOME` must be created by the
  entrypoint, not baked into the image layer.
- `/work` is a host bind mount owned by a **different uid** than the container
  user, so git refuses to operate on it until told otherwise.
- Any state written outside `/work`, `/tmp`, and `$HOME` will fail with `EROFS`.

---

## Reference implementation: the kew-agent image

`docker/agent.Dockerfile` + `docker/agent-entrypoint.sh` are the reference
implementation of this contract, and the image every claude route currently
runs on. A future opencode (or other) image is built against the same five
elements.

**Image layout** (`docker/agent.Dockerfile`):

- `debian:bookworm-slim`, pinned by digest for reproducibility.
- Non-root user `agent`, **uid 1001** — the uid the tmpfs `HOME` mount is
  pinned to. The Dockerfile and `AGENT_HOME` / `_AGENT_UID` in
  `kew/src/kew/sandbox.py` must be kept in sync; a mismatch makes `/home/agent`
  unwritable and breaks the agent at shell-init.
- `/work` created and chowned to `agent` **before** `USER agent`, so the image
  is usable even when `/work` is not a bind mount.
- Preinstalled tooling so agents do not re-download it into the bind-mounted
  checkout every dispatch (#762): `git`, `curl`, `ca-certificates`, `procps`
  (`ps`), `jq` (the security-guard hook parses its payload with it and fails
  closed without it), Node.js 22 + `npm`, `uv` in `/usr/local/bin`, and the
  Claude Code CLI.
- `ENTRYPOINT ["/usr/local/bin/kew-agent-entrypoint"]`, `WORKDIR /work`, and no
  baked credentials — ever.

**Entrypoint** (`docker/agent-entrypoint.sh`), in order:

1. `set -euo pipefail`.
2. Resolve `PROMPT_FILE="${KEW_PROMPT_FILE:-/work/.kew-prompt.txt}"`; exit `64`
   with a stderr message if it is missing or empty.
3. **Git bootstrap** (#742) — required by the read-only/tmpfs constraints
   above, not optional polish:
   ```bash
   git config --global --add safe.directory /work
   git config --global user.name  "kew-agent"
   git config --global user.email "agent@kew.local"
   ```
   Without `safe.directory`, git rejects the bind mount as *dubious ownership*;
   without an identity, `git commit` dies on *Author identity unknown*. Either
   way the agent does the work and it never lands — the kew-721 incident. The
   identity is container-local; the host broker owns push and PR.
4. Print `KEW_AGENT_START <KEW_AGENT_START_NONCE>` followed by one newline.
5. `exec claude --print "$@" <"$PROMPT_FILE"` — the extra-args slot and the
   stdin handoff in one line.

**Verification.** `kew sandbox smoke` exercises launch, mount, and the prompt
handoff without invoking the agent, by overriding the image `ENTRYPOINT` and
`cat`-ing `/work/.kew-prompt.txt` back out. An image that passes smoke has
elements 1–3 wired correctly.

### When the runtime replaces the entrypoint: `kew-openshell-launch`

Under `[sandbox] runtime = "openshell"` the `ENTRYPOINT` above never runs —
`openshell sandbox exec` replaces it. The contract is unchanged; only the
boundary moves. Every agent image therefore installs
`docker/kew-openshell-launch`, and the host invokes it directly:

```bash
openshell sandbox exec --policy docker/openshell/policy-<runner>.yaml ... -- \
    /usr/local/bin/kew-openshell-launch <startup-nonce> <program> [runner args...]
```

For `docker/agent.Dockerfile` and `docker/agent-opencode.Dockerfile` the
launcher is an *additional* launch path, alongside the entrypoint each still
ships for the docker route. For `docker/agent-codex.Dockerfile` it is the
**only** one: that image is OpenShell-only and ships no entrypoint at all, so
anything the codex run needs before dispatch exists only if the launcher does
it.

`<program>` is the **absolute governed path** of the runner (`/usr/bin/claude`,
`/usr/bin/codex`, `/usr/bin/opencode` — the binaries each runner's OpenShell
policy governs), never a bare name: the launcher refuses, without
acknowledging, one that is not absolute or that resolves inside the checkout. The exec runs under a fixed `PATH`
(`/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin`), never the
host's, so no committed `bin/claude` or root `uv` can stand in for the governed
binary; the review environment's install names `/usr/local/bin/uv` the same
way.

It does the same work in the same order, with three differences the runtime
forces:

- **The startup nonce is an argument, not an environment variable.**
  `openshell sandbox exec` owns the child's environment, so an exported
  `KEW_AGENT_START_NONCE` is not guaranteed to reach the launcher. A nonce that
  is not one `[A-Za-z0-9_-]` token is refused before any acknowledgement is
  printed, so no caller can forge extra lines onto the event stream.
- **`HOME` is repointed out of the checkout.** OpenShell sets `HOME` to the
  image workdir, which is the bind-mounted checkout — left alone, `~/.gitconfig`
  and every runner config directory would be written into the tree kew
  publishes. The launcher sets `HOME` (`KEW_AGENT_HOME`, default `/home/agent`)
  and `XDG_CONFIG_HOME` beneath it, and refuses to launch — again without
  acknowledging — if that still resolves inside the checkout.
- **Per-runner pre-dispatch work is a branch, not a whole file.** Each image
  entrypoint serves one runner and hard-codes its own; the single launcher
  serves them all, keyed on the program name the host passes (a program with no
  branch of its own gets its argv through untouched):
    - `claude` — `--print` is injected ahead of `"$@"`, exactly as
      `docker/agent-entrypoint.sh` does. The flag belongs to the prompt handoff
      (element 2) and so is image-owned: `ClaudeCodeRunner.container_args()`
      deliberately omits it, and without it `claude` would start an interactive
      session, read nothing from the piped prompt file, and emit no
      `stream-json` events for the host to parse.
    - `opencode` — the catalog is probed before the prompt is handed over, as
      `docker/agent-opencode-entrypoint.sh` does: a model absent from the
      pinned image's catalog fails closed with an actionable, model-specific
      diagnostic and no model call, rather than reaching OpenRouter and coming
      back as a generic server error.
    - `codex` — a **placeholder** `$HOME/.codex/auth.json` is written before the
      prompt is handed over. Codex takes no credential from the environment: it
      reads that file and exits before any model call if it is absent or
      misshapen. There is no codex entrypoint to mirror, so this branch is the
      only place the file is ever written. What it writes is the shape Codex
      accepts and no more — `auth_mode: chatgptAuthTokens`, an unsigned
      `id_token`, an **empty** `refresh_token` — at mode `0600` under the moved
      `HOME`, so it never reaches the published checkout and is not world
      readable inside the sandbox either. The placeholder values are the host
      provider's to name (`KEW_CODEX_ACCOUNT_ID`, `KEW_CODEX_ACCESS_TOKEN`), a
      forwarded value shaped like a real credential is refused without
      acknowledging, and the access token is held to an **exact** contract —
      the built-in placeholder or `kew-openshell-broker-placeholder`, nothing
      else, since no list of credential shapes can prove an opaque value is not
      one: the OpenShell pilot runs with **no token inside the
      sandbox**, and that is a property of the launcher rather than of its
      defaults.

It also exports `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`: the per-runner
OpenShell policies allow **inference endpoints only** — every endpoint is
`enforcement: enforce`, with no telemetry host and no `github.com` — so
non-essential traffic would otherwise spend the run retrying a blocked host.

---

## Implementer checklist

To be containable by kew, an image MUST:

- [ ] Do all of its work under `/work`, and assume no other host path exists.
- [ ] Default its prompt file to `/work/.kew-prompt.txt`, honour
      `KEW_PROMPT_FILE` as an override, and feed that file to the agent on
      **stdin** — never in argv.
- [ ] Exit non-zero with a stderr message when the prompt file is missing or
      empty, rather than launching a promptless agent.
- [ ] Forward `"$@"` to the agent binary unmodified, and hardcode no flag the
      host owns (model, permission mode, output format).
- [ ] Emit the nonce acknowledgement immediately before agent exec.
- [ ] After acknowledgement, emit **one JSON object per line** on stdout, in the
      vocabulary its runner's `parse_line()` reads, and keep human-readable
      diagnostics on stderr.
- [ ] Pass the runner's terminal result event through stdout unmodified, for
      reviewer runs: it carries the verdict.
- [ ] Run as a **non-root** user whose uid matches the `AGENT_HOME` tmpfs pin
      (`1001` today), and survive `--read-only` + `--cap-drop ALL` +
      `no-new-privileges` + `--pids-limit`.
- [ ] Bootstrap git in the entrypoint (`safe.directory /work` + a
      container-local identity), because `HOME` is a fresh tmpfs every run.
- [ ] Authenticate from **environment variables only**, declared on the runner
      (`required_credentials`) so kew can forward them by name; assume no
      mounted credential file and no CLI login state. Only the *resolved
      runner's* declared names arrive — an image must not expect another
      runner's credentials to be present.
- [ ] Bake **no** credentials into any layer, and expect **no** GitHub token.
- [ ] Pass `kew sandbox smoke`.

And an image MUST NOT:

- Require a writable root filesystem, extra bind mounts, or added capabilities.
- Push, open PRs, or otherwise talk to GitHub — the host brokers all of it.
- Print non-JSON prose on stdout, except the required startup acknowledgement.
- Filter, reorder or synthesize the runner's result events.

## See also

- [How it works — sandboxed dispatch](how-it-works.md#sandboxed-dispatch)
- [Security](security.md) · [Threat model](threat-model.md)
- [Configuration — `[sandbox]`](configuration.md#sandbox)
- [Runners](runners/index.md)
