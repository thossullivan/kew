# Official Codex runner

kew supports the official OpenAI Codex CLI as an explicit host runner for
standard issue dispatch. Install version 0.145.0 or newer and authenticate the
CLI itself:

```bash
npm install -g @openai/codex
codex --version
codex login
codex login status
```

kew checks both the minimum version and login status before launch. It never
reads, copies, mounts, prints, or records the CLI credential. Authentication is
owned by the official CLI login cache or operating-system keychain; kew does
not fall back to `OPENAI_API_KEY` or `CODEX_ACCESS_TOKEN`.

## Configuration

Codex is never inferred from a model name. GPT, o-series, and models with
Codex in their names continue to infer the `opencode` runner. Select the
official runner explicitly with a nonempty, governed-priced model:

```toml
[routing]
default = "claude-code"
simple = { runner = "codex", model = "gpt-5.6-sol" }

[agent.codex]
reasoning_effort = "high"
service_tier = "fast"
```

`reasoning_effort` is optional and accepts `minimal`, `low`, `medium`, `high`,
or `xhigh`. `service_tier` is optional and accepts only `fast`; leave it unset
to preserve Codex's default behavior. Fast mode is governed rather than
inherited from personal configuration: kew passes both
`-c service_tier="fast"` and `-c features.fast_mode=true` even though Codex runs
with `--ignore-user-config`. The official CLI remains responsible for rejecting
an account or model that is not eligible for Fast mode.

For example, an explicitly routed `gpt-5.6-luna` run may combine
`reasoning_effort = "xhigh"` and `service_tier = "fast"`. Confirm authenticated
model availability before dispatch and add a reviewed pricing override if the
model is not in kew's shipped pricing table. `[agent.codex]` intentionally has
no `extra_args` escape hatch.

Codex usage is valued as **API-equivalent included-plan value**, never described
as free and never presented as native billed dollars.

Codex JSONL is the only source used for reported model and service-tier
identity: kew reads those fields from the structured response envelope and
never searches agent messages or command output for them. kew persists the
governed `requested_service_tier` alongside the structured
`reported_service_tier`; a missing reported value remains missing rather than
being inferred. For adaptive Switchyard runs, the admitted route is retained
separately from the served model and selected Switchyard tier. The run report
also identifies classifier overhead as proxy observation evidence, while
runner-reported usage remains authoritative for task accounting.

## Execution and security

Codex uses Codex-native `workspace-write` on the host; on a gateway-launched
`openshell` dispatch it applies that same sandbox inside kew's container instead
of on the host ([Posture per runtime](#posture-per-runtime)). Command network
access is off, `.git` is protected, and extension, web-search,
shell-environment, and secret inheritance controls are fixed by kew either way.
On the host lane the Codex process keeps `HOME` so the official login can work,
while `OPENAI_API_KEY`, `CODEX_ACCESS_TOKEN`, unrelated provider credentials,
GitHub tokens, and AWS credentials are removed from its process environment.
A native Codex run (`runtime = "docker"` or `"podman"`) can read any file the
host user can read, including credential files such as `~/.codex/auth.json`,
and writes only its checkout and a temp directory of its own: kew makes that
directory under the work root, sets it as `TMPDIR`, and tells Codex to leave
the shared `$TMPDIR` and `/tmp` out of its writable roots.

Project policy is deliberately narrow:

- regular files under `.codex/rules/` and `.codex/skills/` are visible;
- symlinks, special files, unknown `.codex` entries, and
  `.codex/config.toml` fail closed; and
- personal `.codex` content is neither copied nor mounted by kew.

The runner declares `publication_style="host"`. After a clean exit, kew owns
commit, push, pull-request creation, and the dispatch report. An exit-zero run
that cannot complete that host broker is a failed dispatch, not success.

The containment posture recorded for this host execution is `native`. Codex
applies its own sandbox instead of kew's external container boundary, and the
runner protocol declares that boundary in code; configuration cannot grant
native status. `required_credentials == ()` is intentional because native
execution uses the official CLI login and never enters the container credential
injection path. No CLI-login-cache mount is supported for Codex, and there is
no Docker lane for an image of its own.

### Posture per runtime

The declaration is read per runtime, so the posture depends on which boundary
kew is actually offering:

| `[sandbox] runtime` | Posture | Who holds the boundary |
| --- | --- | --- |
| `docker`, `podman` | `native` | Codex's own `workspace-write` on the host |
| `openshell`, with the gateway CLI installed | `sandboxed` | kew's OpenShell sandbox, with `workspace-write` nested inside it |
| `openshell`, on a run the gateway cannot launch | `native` | Codex's own `workspace-write` on the host |
| `openshell`, with no gateway CLI on this host and `dangerously_skip_containment = true` | `native` | Codex's own `workspace-write` on the host |
| any runtime, continuing a preserved checkout | `native` | Codex's own `workspace-write` on the host |

Without `dangerously_skip_containment`, a host with no gateway CLI refuses a
Codex dispatch on `openshell` with `Sandbox runtime 'openshell' not found`,
exactly as it refuses every other runner. Naming the runtime asked for its
boundary, so kew does not quietly fall back to Codex's own sandbox instead.

On `openshell` kew creates the sandbox from the image mapped at
`[sandbox] images.codex`, governed by the packaged egress policy for this
runner, and runs `codex exec --json` through the in-sandbox launcher. The
gateway holds the credential in the provider named at
`[sandbox.openshell] providers.codex` and injects it at the endpoint that
policy permits, so the empty `required_credentials` is still the accurate
declaration: no credential name is forwarded into the sandbox, and the
ChatGPT-mode `auth.json` the launcher writes there is a placeholder. Admission
needs a fresh egress certification like every other dispatch on that runtime,
and this runner's own boundary does not waive it.

Every Codex launch names a model — host, resumed or contained. A dispatch with
no routed model is refused rather than handed to the CLI's own default, which
would be billed and attested as the routed one.

On `docker` and `podman` the posture stays `native` and no container is
launched: the packaged Codex agent image is built for the OpenShell lane, and
its launcher refuses to serve as an entrypoint under the Docker runtime.

The last three rows are the same rule read backwards: Codex only declines its
own seatbelt where kew is really offering a boundary for *that* run, so it is
never refused over one neither side was going to hold.

Three kinds of run reach the gateway runtime with no launch path on it: the
remediation flows (`kew review`, `kew resolve`, `kew ci-fix`, an epic's plan
run), which swap one execution step for a container command line the gateway has
nowhere to put; any sidecar Switchyard topology, whose worker container and
network filter go through the Docker daemon; and a managed Switchyard transport
with no sidecar, whose endpoint the gateway exec cannot carry and the packaged
egress policy does not permit. kew holds no boundary for those on `openshell`,
so Codex keeps its own and they run `native` exactly as they do on `docker`.

A runtime **name** in the config file is a request, not a boundary. With no
`openshell` CLI on the dispatching host there is nothing to defer to, so the
posture is `native` there too — the same answer `runtime = "docker"` gives on a
machine without Docker.

Continuing a preserved checkout (`kew epic continue`) is host-only on every
runtime: the existing worktree cannot be handed to a container without replacing
the checkout you asked to keep. Codex therefore continues `native`. A runner
that holds no boundary of its own is still refused there, with
[`reason = "continuation_host_only"`](../audit.md).

Setting `[sandbox] enabled = false` has the same effect for the same reason.

## Canary and resume

The standard canary checks fresh JSONL parsing, positive token usage, session
capture, terminal completion, classification, derived pricing, tree
cleanliness, and session deletion:

```bash
kew canary --runner codex --model gpt-5.6-sol --json
kew canary --runner codex --model gpt-5.6-sol \
  --exercise-resume --json
```

The resume gate interrupts a fresh thread, resumes it through the official CLI,
and deletes the session afterward. A Codex `needs_human` signal is surfaced as
a governed pause. Raw `--record` JSONL is sensitive evidence and is not a
credential store.

## Supported and refused flows

- **Supported:** standard issue dispatch, conflict resolution, and standalone `kew review`,
  all with host-brokered publication.
- **Refused in v1:** first-class loops, which do not yet own a compatible host
  publication broker.
- **Production adversarial review:** remains externally contained OpenCode.
  Selecting Codex there fails closed on every runtime, `openshell` included,
  and for one runtime-independent reason: that reviewer refuses a `[review]
  runner` whose `required_credentials` is empty before it touches a worktree
  or a container, and Codex declares none on any lane. The gateway lane gives
  dispatch a kew-held boundary; it does not give this reviewer a credential to
  check. The dogfood reviewer remains pinned to OpenCode; other
  credential-declared contained reviewers remain structurally eligible.
