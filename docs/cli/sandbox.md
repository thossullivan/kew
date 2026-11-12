# kew sandbox

Tooling for the sandboxed-dispatch container path.

```text
Usage: kew sandbox COMMAND [ARGS]...
```

## Recorded posture

Every `sandbox_resolved` event carries `attempt_number` and a unique `posture_id`.
Direct contained routes retain `operator-managed-unverified` / `operator-asserted`.

Regular dispatch and the four agent flows share the sidecar lifecycle:

1. Persist the retry's attempt number before writing pending posture.
2. Write `unavailable` / `pending-sidecar` and read that exact event back before setup.
3. Admit both the worker and its route profile.
4. Write `sidecar-certified` / `sidecar-admission` and read that exact event back before worker execution.

An absent pending record refuses setup.
An absent promotion record refuses the attempt before worker execution.
The refusal names the missing durable posture and emits `sandbox_posture_refused` with its attempt number.
Refusal recording remains subject to audit-store availability.

Run details, JSON and HTML evidence packs, and the TUI select the latest attempt's newest posture event.
They reject earlier attempts' certification even when someone appends that stale event later.
Missing current-attempt posture renders pending posture and unavailable egress.
Legacy events without an attempt number apply only to attempt one.
The TUI refreshes selected posture each second without requiring another selection.

## kew sandbox build

Build a runner image with `docker build` from inputs shipped inside `kew-cli`.
No source checkout is required.

The default tag is the digest pin for the packaged inputs.
Claude Code uses `kew-agent:docker-<digest12>`.
OpenCode uses `kew-agent-opencode:docker-<digest12>`.
Codex uses `kew-agent-codex:docker-<digest12>`.
Use `--tag` with `--unpinned` to select another output tag.

```text
Usage: kew sandbox build [OPTIONS]

Options:
  --runner TEXT  Packaged image runner: claude-code, codex or opencode.
  --tag TEXT     Output tag. Defaults to the packaged input digest pin.
  --unpinned     Allow --tag to differ from the packaged input digest pin.
```

The digest covers each expected filename, byte sequence, and executable bit.
The built image carries the full digest as the label `dev.kew.agent-image-inputs`.
Before a container is created for a `docker-<digest12>` tag, kew reads that label
and refuses an image whose label is missing or names other inputs.
The command validates the packaged manifest before any Docker action.
It refuses links, special files, missing files, extra files, and mode changes.

An explicit tag must equal the digest pin by default.
`--unpinned` permits another tag for local inspection.
The command warns that such a tag is not the packaged pin.
Dispatch runs it only when `[sandbox] image` or `images.<runner>` names it.
With `[sandbox] network = "open"`, the command reports full egress instead of the network check.

The command checks whether the `kew-allowlist` network exists.
It never creates or modifies that network.
If the network is absent, the command prints provisioning instructions.
Provision a restricted network for approved inference endpoints and GitHub.
A plain bridge has unrestricted egress and is not an allowlist.

Success output includes the tag and the full immutable image ID.
The command checks daemon availability before starting the build.
Exit code `2` means a prerequisite prevented the build.
Exit code `1` means the build ran and failed, including image verification failures.

| Exit code | Build outcome |
| --- | --- |
| `0` | Image built and verified. |
| `1` | Build ran and failed. |
| `1` | Built image verification failed. |
| `2` | Docker missing. |
| `2` | Docker daemon unreachable. |

```bash
kew sandbox build
kew sandbox build --runner opencode
kew sandbox build --tag local/claude:test --unpinned
```

## kew sandbox network

Create the `kew-allowlist` network that contained dispatch attaches to, on the
`[sandbox] runtime` you configured (`docker` or `podman`).

```text
Usage: kew sandbox network [OPTIONS]

Options:
  --check  Report whether the network exists; create nothing.
```

The command creates a bridge network: `<runtime> network create --driver
bridge kew-allowlist`. That is the network the doctor's `allowlist-network`
check looks for, so the check passes once the command has run.

It is idempotent. When `kew-allowlist` already exists, the command reports it
and changes nothing, whatever the network's shape. A create that races another
creator reads the network back and reports it present.

kew installs no firewall rules on the network. A plain bridge gives the
container full egress while it holds your provider key, and the command warns
when the network it created or found is one. Restrict it to your inference
endpoint and GitHub yourself.

On `[sandbox] runtime = "openshell"` the command creates nothing and says why:
OpenShell enforces its own egress policy and attaches no network. Prove that
boundary with `kew sandbox certify` instead.

| Exit code | Outcome |
| --- | --- |
| `0` | The network was created, already exists, or the runtime is OpenShell. |
| `1` | `--check` found the network absent. |
| `2` | Runtime missing or unresponsive, the create failed, or `[sandbox] network` is not `"allowlist"`. |

**Example:**

```bash
kew sandbox network
kew sandbox network --check
docker network inspect kew-allowlist
```

## kew sandbox certify

Prove a container runtime's egress is closed, and record the proof on the audit
chain. Required before any dispatch on `[sandbox] runtime = "openshell"`:
admission refuses that runtime without a passing certification no older than
24 hours for the exact boundary it would launch.

```text
Usage: kew sandbox certify [OPTIONS]

Options:
  --runtime TEXT  Container runtime whose egress to certify: openshell.  [required]
  --runner TEXT   Runner whose image and OpenShell policy are certified.
  --flow TEXT     Which launch's boundary to certify: dispatch (the default) or
                  review, the adversarial reviewer's narrower policy.
```

The command reads the runner's own policy under `docker/openshell/` once,
then creates a throwaway sandbox from the runner's immutable image ID for
**each binary that policy governs**, runs one probe inside it, and deletes it on
every path. A policy grants egress per binary path, and the runner's native
binary (for example `claude.exe`) cannot run a probe program, so each probe runs
as `/usr/bin/node` under a policy that grants node exactly that binary's clause
— the shipped header and endpoint list, with only the path changed. Every
governed binary's matrix has to pass: a policy whose runner clause alone was
widened fails, even though node's own clause is untouched. Every probe sandbox
is created from a private copy of the bytes that were read and hashed, never
from the packaged path a second read could find replaced.

### One runtime, more than one boundary

`[sandbox] runtime = "openshell"` names one container boundary per *launch*, not
per runtime, and `--flow` says which launch:

| `--flow` | Policy | Egress it permits | Certifiable |
| --- | --- | --- | --- |
| `dispatch` (default) | `policy-<runner>.yaml` | the runner's inference hosts | yes |
| `review` | `policy-review-<runner>.yaml` | the inference host only | yes |
| `review-provision` | `policy-review-provision-<runner>.yaml` | the package index hosts only | no |

The adversarial reviewer is a separate boundary because its prompt requires it
to **execute the reviewed PR's tests**. A proof that a dispatch boundary holds
says nothing about it, so the review certification is bound to the review
policy's own digest: a reviewer on `openshell` is refused with
`openshell-egress-uncertified` until `--flow review` has run, and a dispatch
certification does not satisfy it. Only `claude-code` can review
(`[review] runner` must resolve to it), so only `claude-code` ships the review
policies.

`review-provision` is the sandbox the reviewer's environment is installed in —
`uv sync` against the PR's lockfile, which runs the PR's own build backend, with
no OpenShell provider at all and therefore no credential in the sandbox, not
even the placeholder a dispatch carries. Its policy governs `uv` alone, so the
build backends uv executes reach no host whatever. That is also why it cannot be
certified: the probe is a Node program, node is deliberately ungoverned, and a
probe run there would report a failed control for a policy working exactly as
written. `--flow review-provision` says so and runs no probe, rather than
producing a failure that reads like a leak.

The probe records three positive controls and eight denials:

| Probe | What it dials | Passing result |
| --- | --- | --- |
| positive control | the runner's inference API host, TLS 443 | reachable |
| `udp_dns` control | the sandbox's own resolver from `/etc/resolv.conf`, UDP 53 | reachable |
| `ipv6_external` control | the same inference host, TLS 443, IPv6 only | reachable |
| `external_host` | a documentation host outside the policy, TLS 443 | denied |
| `public_ip` | a routable public address, no DNS, TCP 443 | denied |
| `udp_dns` | a public resolver, UDP 53 | denied |
| `ipv6_external` | an external address over IPv6, TCP 443 | denied |
| `link_local_metadata` | `169.254.169.254`, TCP 80 | denied |
| `host_gateway` | the sandbox's own default gateway, OpenShell's gateway port | denied |
| `github` | `github.com`, TLS 443 | denied |
| `unresolvable_name` | a reserved `.invalid` name | does not resolve |

The positive control is what makes the denials evidence. A sandbox with no
working network denies everything, so without a proven reachable inference host
a matrix of eight denials certifies nothing — and the certification fails rather
than passing on that matrix.

That main control is TCP over IPv4, which proves nothing about a sandbox with no
UDP path or no IPv6 route: either one denies its whole destination class
whatever the policy enforces. `udp_dns` and `ipv6_external` therefore each carry
their own same-protocol control, and a leg whose control did not hold fails the
certification. This is deliberately the fail-closed direction — a boundary whose
IPv6 is merely absent is refused rather than certified on a leg kew never tested
for enforcement — and the command prints each control's own detail, so the
output names which one to fix.

A connection that is refused or reset counts as **reachable**: a packet that
drew an answer left the boundary. Only an unreachable network, a local rule, no
DNS answer, or silence until the deadline counts as a denial. A probe that could
not be attempted records `unavailable`, which fails the certification; it never
shortens the matrix.

The recorded `runtime_egress_certification` event (schema 2) carries one
matrix per governed binary and binds them to five facts, and admission matches
on all five: the runner's immutable image ID, the SHA-256 of the policy bytes
the probes ran under, the version the OpenShell CLI reported, a SHA-256 of the
gateway's own identity (the endpoint kew dials, plus the address, version and
supervisor and sandbox images the running gateway reports under
`openshell status -o json`), and the SHA-256 of the launcher
`/usr/local/bin/kew-openshell-launch` as read **inside the image** by the probe.
An image whose launcher is not the one this kew ships is refused with a rebuild
remedy rather than certified. Change any one of the five and the proof no longer
describes what a dispatch would launch, so a new certification is required; a
schema-1 proof recorded before this binding existed matches nothing.

The launch checks again, at the moment it creates the sandbox: it reads the
policy once, matches a certification on the digest of that read and on the
gateway client it is about to use, and hands the gateway a private copy of the
same bytes. A policy swapped after admission, or a gateway other than the one
certified, is refused there.

A probe sandbox kew could not delete makes the run a failure even when every
matrix held: nothing is recorded, and the output names the sandbox to delete.
It is reported as an operational failure, never as a boundary that leaked.

An OpenShell dispatch with a matching certification records
`egress_verification = openshell-certified` with
`egress_verification_basis = openshell-selftest`. Without one it is refused with
`openshell-egress-uncertified`, before the runtime is probed, because this is
the one OpenShell refusal a shipped command clears.

This command certifies the runners kew packages a policy for: `claude-code`,
`codex` and `opencode`. `pi` and `hermes` have none — there is no
`policy-pi.yaml` or `policy-hermes.yaml` — so for them there is no boundary to
probe: `--runner pi` fails with `has no OpenShell policy to certify`, and a
dispatch of either on `[sandbox] runtime = "openshell"` is refused as
`openshell-policy-unpackaged` rather than as an unproven boundary. That refusal
names the missing policy and names no command on purpose: packaging one is kew's
job, and telling the operator to certify would point at a run that can only fail
the same way.

There is no *pending* OpenShell posture — nothing later upgrades a recorded one.
Every flow reads this certification off the same ledger before it claims the
boundary. Only dispatch can then launch into an OpenShell sandbox: review
remediation, conflict resolution, CI fix and plan run launch their agent as a
container command line, which a gateway sandbox does not have, so they are
refused at admission (or run on the host under `enabled = "auto"`, for review and
resolve). A contained OpenShell dispatch records
`egress_verification = openshell-certified` beside
`egress_verification_basis = openshell-selftest`. A posture kew holds no proof
for records `egress_verification = unavailable` beside
`egress_verification_basis = openshell-uncertified`: the ledger says kew holds
no proof for that boundary rather than borrowing the direct route's permanent
`operator-managed-unverified`.

| Exit code | Certification outcome |
| --- | --- |
| `0` | Every probe passed; the certification is on the audit chain. |
| `1` | The probe ran and the boundary did not hold; nothing was recorded. |
| `2` | The probe could not be run, or it passed and the proof could not be recorded (no gateway, no image, no packaged policy for the runner, unknown runtime, unwritable ledger). |

A boundary that held and a proof that could not be appended are two different
failures, and only the first is a certification failure. An append the ledger
refuses leaves the boundary unproven for admission exactly as an absent proof
does, so it is reported as an operational failure and never as a leak.

```bash
kew sandbox certify --runtime openshell
kew sandbox certify --runtime openshell --runner opencode
kew sandbox certify --runtime openshell --runner codex
# the adversarial reviewer's own boundary — required before a review on openshell
kew sandbox certify --runtime openshell --flow review
```

## kew sandbox smoke

Launch one container end-to-end and report the first failing layer. This proves
the LIVE sandbox path that unit tests can't: runtime resolution, image presence
(built from packaged inputs when a standard tag is absent), container launch,
and the
prompt-file handoff, plus a probe that the agent user's HOME is writable
(the tmpfs mount that keeps Claude Code's Bash tool and git working under the
read-only root, #742). The container only echoes the mounted prompt file and
touches its own HOME, so **no API key is required** for those layers. A final
env-only `auth` layer then
checks that a real dispatch could authenticate: containers cannot inherit CLI
login, so either `ANTHROPIC_API_KEY` or `CLAUDE_CODE_OAUTH_TOKEN` (from
`claude setup-token`) must be exported.

```text
Usage: kew sandbox smoke [OPTIONS]

Options:
  --runner TEXT  Runner whose containment to prove; its image comes from
                 [sandbox] images.<runner> (default: claude-code).
  --no-build     Fail (don't build) if the configured image tag is absent locally.
```

Exit code `0` means success. Exit code `2` means the runtime prerequisite is missing.
Exit code `1` means the smoke ran and a later layer failed. Failures name the failing layer.

| Exit code | Smoke outcome |
| --- | --- |
| `0` | Smoke completed with success. |
| `2` | Missing runtime prerequisite (`runtime` layer). |
| `1` | Smoke ran; the `image`, `launch`, `execution`, or `auth` layer failed. |

The image is resolved through the same runner-aware resolver dispatch uses
(`[sandbox] images.<runner>`, falling back to `[sandbox] image` for
`claude-code`), so the smoke always proves the image that a dispatch of that
runner would actually launch. A runner with no mapped image fails at the
`image` layer naming the key to set, and the `auth` layer checks the
credential *that runner* needs in-container. Smoke uses the same packaged
builder for the standard Claude Code, Codex and OpenCode tags. A custom image remains
yours to build or pull.

**Example:**

```bash
# Build a standard image if needed, then smoke-test the container path
kew sandbox smoke

# Require the image to already be present (no local build)
kew sandbox smoke --no-build

# Prove the image a non-claude route will run, before routing work at it
kew sandbox smoke --runner opencode
```

### Prompt handoff

The prompt reaches the agent through a mounted prompt file, not argv: the host
writes it to `<checkout>/.kew-prompt.txt` (mounted at `/work/.kew-prompt.txt`),
and the image entrypoint feeds it to `claude --print` on stdin. See
`kew/src/kew/sandbox.py` and `docker/agent-entrypoint.sh` for the full contract.


### Bounded OpenRouter credentials

Every direct OpenRouter route requires `OPENROUTER_PROVISIONING_KEY` in the host environment.
This applies on the host and in a container.
A plain `OPENROUTER_API_KEY` does not satisfy it.
Kew issues `OPENROUTER_API_KEY` for each run and forwards it by name only.
The launch replaces the host inference value without changing the host environment.
The provisioning value never enters an agent process, container, argv, log, or audit record.
Discovery probes also exclude the provisioning credential.
Detached issuance applies only to direct OpenRouter routes.
The parent resolves the item's runner and inference transport before issuance.
Native Claude, native Codex, and Switchyard routes require no bounded key or provisioning credential.
Switchyard sidecars remain excluded when their upstream route uses OpenRouter.
These routes receive no issued-key marker and retain their existing upstream authentication.
Issuance and refusal events record the route decision in `credential_route`.
The driver mints each detached run key before spawning `kew epic _dispatch-node`.
This includes delegation runs.
Children never hold management authority.
Every launcher removes `OPENROUTER_PROVISIONING_KEY`, including launchers for Kew itself.
The parent supplies the bounded secret, `KEW_ISSUED_RUN_KEY_HASH`, and `--run-id`.
The child verifies the issuer hash and secret fingerprint against that run's durable issuance event.
The child records phase `inherited` and neither mints nor revokes the inherited key.
A marker on any excluded route also refuses inheritance.
A mismatch refuses launch with `issued credential mismatch` and publishes nothing.
Missing provisioning or failed issuance refuses direct OpenRouter detached dispatch and releases its reservation.
The parent never substitutes the host inference key after an issuance failure.
Generic helpers strip the bounded key and marker when the dispatch process holds an issued-key marker.
Only explicit dispatch handoff, agent launch, and container launch receive the issued key.
Container arguments forward the key by environment name; helpers such as `docker inspect` receive no bounded key.

Each key has a finite dollar limit, `limit_reset: null`, and a UTC expiry.
Kew includes BYOK spend in that limit.
The limit uses the effective run cap, including review or remediation overrides.
Without a run cap, Kew uses the configured daily remaining budget.
Expiry uses UTC and covers the complete bounded retry schedule.
Lifetime is `attempts * timeout + 1.25 * sum(backoff intervals) + 300 seconds`.
Tracked runs use `retry.max_attempts` and the effective per-attempt timeout, including overrides.
Backoff intervals are `retry.backoff_base_seconds * retry.backoff_multiplier**i`, for `i` from zero through `attempts - 2`.
The factor 1.25 covers maximum retry jitter. The final 300 seconds provide cleanup grace.
The shared container streamer has one four-hour attempt and no retry backoff.
Kew refuses launch when the remaining retry schedule exceeds the remaining key lifetime.
Kew also checks each attempt's timeout immediately before process execution.
Nonfinite or unrepresentable lifetimes are refused before issuance.
Retries share one key and one total spend ceiling.
Detached key revocation runs on the next driver tick after terminal status or expiry.
The reconciler reads bounded pages through an issuance index and indexed run lookups.
Large backlogs can require additional ticks.
Successful revocations are skipped; failures retry at most once per hash per tick.
Driver exit also attempts reconciliation, including exceptional exits.
Active detached children keep their keys when the parent exits.
The standalone delegation watcher reconciles terminal and expired keys once per pass.
Watcher exit reconciles again, including exceptional exits, while preserving running children.
A shared seen set prevents duplicate revocation attempts within one pass and its exit cleanup.
If no driver or watcher returns, the key's own expiry is the backstop.
The parent records issuance with `issued_by="driver"` and revocation through `sandbox_resolved` events.

Foreground runs revoke the key at run end, including failed and interrupted runs.
Expiry bounds failures where revocation cannot complete.

The route profile records the key hash and bounds, never the key value.
The `sandbox_resolved` issuance and revocation events record the same identity.
Revocation records `credential_issuer.revoked` and a safe failure reason when needed.
The profile uses `issued:openrouter:<hash>` and requires matching attempt issuance evidence at launch.
Publication requires recorded successful revocation; missing or failed revocation holds publication with `issued_key_unrevoked`.
A later retry releases publication after successful revocation evidence arrives.
Raw ambient `env:` credentials publish only a head the host scanned clean immediately before the push.
See [Publication output scan](../security.md#publication-output-scan).
Direct OpenRouter runs require host `OPENROUTER_PROVISIONING_KEY` before launch, contained or not.
Missing provisioning causes `OpenRouter provisioning key required for direct routes` and exit 2.
The refusal records a `refused` run row and releases its reservation.
See [OpenRouter setup](../runners/openrouter.md#setup) to configure the provisioning key.
Every container environment removes ambient `OPENROUTER_API_KEY` before explicit bounded-key injection.
Uncontained/native host launches also remove ambient OpenRouter credentials.
The audit records `credential_issuer: null` with reason `no provisioning key`.
Switchyard, Claude OAuth, and native Codex retain their existing credential handling.
