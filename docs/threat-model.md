---
description: What kew defends against and what it does not, starting from the control-plane host as the trust boundary.
---

# Threat model

This document states, honestly, **what kew defends against and what it does not.** kew
runs autonomous coding agents against your code; that is inherently a position of trust,
and the value of a control plane is bounding the blast radius when something the agent
does goes wrong. Knowing the edges of that boundary is part of using kew safely.

For how to report a vulnerability, see [SECURITY.md](https://github.com/thossullivan/kew/blob/main/SECURITY.md). For the concrete
command-blocking rules, see [the security page](security.md).

## Trust model

- **The control-plane host is the trust boundary.** The machine running `kew` — its OS,
  its filesystem, its environment (where `ANTHROPIC_API_KEY`, `gh` credentials, etc.
  live), and the user account `kew` runs as — is **trusted**. kew does not defend the
  host against itself.
- **The operator is trusted.** kew is a tool you run on your own machine to govern
  *agents*, not to defend against *yourself*. (One consequence — see the audit section —
  is that the governance log is hash-chained and its head can be anchored, so tampering is
  *detectable* (`kew audit verify`). The ledger host computes each anchor; a copy can be
  pushed off the host. But because the host and the GitHub Actions token are
  trusted, a host-level actor can still rewrite history and re-anchor it. So the log is
  **tamper-evident within stated limits, not tamper-proof — kew is not an HSM.**)
- **The runner CLIs are trusted components.** kew shells out to `claude`, `opencode`,
  `codex`, and other registered runners. A malicious or compromised runner binary is outside kew's boundary —
  kew governs what it *asks* the runner to do and what the runner's process is *allowed*
  to do, not the integrity of the runner itself.

## Primary adversary: the agent kew governs

The main thing kew defends against is **the autonomous agent itself misbehaving** — not
an external network attacker. A coding agent is *semi-trusted*: it does useful work, but
it can be **prompt-injected** (via issue text, repository content, dependency READMEs, or
web content it reads), **misaligned**, or simply **buggy and reckless** — especially when
run with `--dangerously-skip-permissions`. kew's job is to make a misbehaving agent's
blast radius **small, bounded, and observable.**

### What kew defends against (mechanism → threat)

| Mechanism | Threat it bounds |
|---|---|
| **Claude Code command guard**, when the posture record says `loaded` — the checkout hook matches the trusted project copy; see [security.md](security.md) for the exact patterns | A Claude Code agent running known destructive or exfiltration command shapes. This is a **denylist**. Other runners do not load this guard. See non-goal #1. |
| **Standalone clone isolation** | Every checkout an agent receives for dispatch, loops, planning, review, resolve, and ci-fix is a `--no-hardlinks` clone with its own `.git`. It is never Git-linked to your repository or main checkout. This removes the implicit Git linkage only. Through Git, an agent cannot write your `.git/config`, drop a hook into your `.git/hooks/`, or share your object store. Legacy linked checkouts are refused on resume and preserved for migration. An agent admitted to the uncontained host path can still write any path your user can. The container boundary bounds that access. The security hook bounds it for Claude Code only. Other runners on the host path have no equivalent. See [security.md](security.md#isolated-checkouts). This limits accidental damage during the run. |
| **Canonical publication** | Kew reads the checkout as plain file data. It filters an artifact and rebuilds the tree in a fresh host-owned bare repository. Host Git never resolves the agent checkout. No Git process rooted in the agent checkout receives the publication credential. Host processes can inherit it: Git in the canonical repository, Git in the operator's own project checkout, and `gh` for PR creation. Before every push, the host scans the added and changed files for the credential values it holds and for known key shapes, and refuses on a match ([publication output scan](security.md#publication-output-scan)). See [security.md](security.md). |
| **Verification gate + diff-guard** (anti-proxy) | An agent *claiming* success it didn't achieve, or editing its own tests / CI config to make a red task look green. `achieved` requires a real gate pass on a diff that did not touch the guarded paths. `kew.loops.verification.CommandCondition.check` runs the operator-configured command (`[commands] verify_loop`, else `[commands] ci`) as `/bin/sh -c` inside an approved image over a host-owned copy of the published commit. Missing containment prerequisites require explicit uncontained host admission (#1444). Canonical `ci_green` mode polls the recorded SHA without local project execution. |
| **Bounded run keys** (direct OpenRouter routes) | A leaked or reused inference key. The host mints each run its own OpenRouter key with a finite, non-resetting spend limit and a UTC expiry, and revokes it at run end; publication waits for the recorded revocation. The provisioning key that mints them never reaches a child process, and a direct route without one refuses before launch. See [security.md](security.md#sandboxed-dispatch-credential-posture). |
| **Proxy-environment control** (`[sandbox] proxy_environment`) | Host proxy URLs, and the credentials embedded in them, reaching an agent container. The default `"none"` assigns all ten proxy variables empty, overriding Docker and Podman proxy defaults; `"inherit"` forwards only the six present HTTP(S)/NO_PROXY names, by name, and refuses a proxy value it cannot parse for redaction. See [configuration.md](configuration.md#sandbox). |
| **Per-run budgets + mid-flight watchdog** (#291) | Runaway spend — a loop is killed when cumulative cost crosses its cap |
| **Wall-clock + step caps** | A loop that never terminates or thrashes — bounded regardless of cost signal |
| **Container resource caps + host watchdog** | Resource exhaustion — cgroups bound memory, CPU, and processes; rlimits bound files; sized tmpfs mounts bound scratch memory; the host watchdog bounds checkout growth and elapsed time. `--init` forwards signals; a container that ignores TERM is stopped by the host watchdog's `docker kill` (`timeout -k` only ends the local client). These caps and this watchdog apply to container runs only. Host runs and runner-native runs, such as the official Codex runner, get no cgroup, rlimit, tmpfs, or checkout-growth cap. |
| **Fleet kill switch** (`kew stop`) | A fleet of agents collectively going wrong — one command terminates the process groups |
| **Audit envelope** (#246) | Unreviewable activity — dispatches, gate verdicts, budget events, and pause/kill are recorded to a local **hash-chained** log (`kew audit tail` / `export`), on a **best-effort** basis (a failed write is logged and dropped, never blocks the agent). Integrity is checkable with `kew audit verify`. The ledger host anchors the chain head and can push the anchor to a repository, so it is **tamper-evident within stated limits** (not tamper-proof — see non-goal #3) |

Each dispatch attempts to write one effective posture record.
It names filesystem, network, credentials, command guard, admission, and refusal reason.
CLI, Serve, TUI, and evidence-pack surfaces read the recorded event.
They show `posture: not recorded` when no event exists.
They do not infer posture from configuration.

## What kew does **not** defend against (explicit non-goals)

These are real limitations, stated plainly so they are footnotes rather than surprises.

1. **The command guard is not a sandbox.** Its denylist covers known command shapes.
   A determined agent can find shapes that it does not cover.
   External containers and runner-native boundaries provide dispatch containment.
   An admitted host run has no containment boundary.
   Run Kew inside a VM or container when Kew itself needs isolation.

2. **Egress control depends on the sandbox; the guardrail hook alone does not bound it.**
   The security hook blocks common exfiltration command *shapes*, but an agent with shell
   access and skipped permissions has many possible network paths, so the hook alone does
   not contain egress. `[sandbox] network` defaults to `"allowlist"`.
   The direct container joins the operator-provisioned `kew-allowlist` network.
   Docker refuses the launch if that network is absent.
   Egress restriction depends on the operator's rules, and an
   **unsandboxed** run — or one that opts into `network = "open"` — has no such control.
   Treat any data an unsandboxed agent can read (your repo, your local filesystem within
   reach, environment it can enumerate despite the hook) as *potentially reachable by the
   model provider and beyond*. Don't point an unsandboxed agent at secrets you can't
   rotate.

   The direct route's egress policy is operator-asserted and unverifiable by kew (#1945).
   Docker's network API exposes no destination rules.
   Docker Desktop keeps the host firewall inside its VM.
   Kew permanently records `operator-managed-unverified` for every direct contained route.
   Operators who need kew-verified egress use the sidecar route, subject to its admission gates.

   *What a sandboxed agent can reach in the first place is bounded by the per-runner
   credential env allowlist* (see [the security page](security.md#the-credential-env-allowlist-is-per-runner)):
   a contained run is handed only the credentials **its own runner** declares, by name,
   so a compromised or prompt-injected opencode container has no Anthropic token to
   exfiltrate and a claude container has no OpenRouter key. This narrows the blast radius
   of a successful injection inside the sandbox; it does not prevent the injection, and it
   does not protect the one credential the runner legitimately needs.

3. **The operator is not an adversary kew defends against — and the audit log is
   tamper-evident, not tamper-proof.** The audit envelope is a **local, hash-chained
   SQLite log**: every event carries `prev_hash` (and a `policy_hash` of the governance
   policy in force). The entry hash covers the id, timestamp, event, run id, issue
   number, repository, actor type, actor id, endpoint, policy hash, payload, and
   `prev_hash`. On every row written from schema v77 on it also covers the row's chain
   format and the `item_id` and `dedup_key` columns. An after-the-fact edit to any of
   these fields breaks the chain and is caught by `kew audit verify`. Rows written
   before schema v77 still leave the `item_id` and `dedup_key` columns out of the hash:
   on those rows an edit to either column does not break the chain
   (#2029). Their hashes are kept
   as they were because the published anchors cover them. You choose the anchor sink. The default is a file at the
   project root. The `.github/workflows/audit-anchor.yml` workflow can commit and
   push it (#249 and #250,
   shipped in M3). The workflow runs on the self-hosted runner that holds the ledger, so
   the anchor is computed on the ledger host. It is not an independent witness. A pushed
   anchor lets a third party detect a later edit to the events it covers. But a
   **host-level actor who controls the GitHub Actions token can still rewrite the log and
   re-anchor it** — kew is not an HSM — and a failed write is still dropped rather than
   blocking work. So it is tamper-evident **within the limits stated in
   [docs/audit.md](audit.md)** (no MAC; the witness is the Actions run log within its
   retention window), not cryptographic proof against the host itself.

4. **The control-plane host and the runner CLIs are trusted.** kew does not defend against
   a compromised host, a malicious local user on that host, a tampered runner binary, or
   theft of the credentials in the host environment. Host hardening and credential hygiene
   are the operator's responsibility.

5. **Prompt injection is not prevented.** kew does not sanitize issue text, repository
   content, or anything else the agent reads. A crafted payload can steer the agent; the
   security hook and the anti-proxy diff-guard bound some *downstream consequences* (it
   can't run blocked commands, and editing its own tests/CI won't fake a pass), but the
   injection itself is not stopped. This is another reason to sandbox (non-goal #1).

### Checkout binding boundary

Checkout binding defends against replacement by another process, including concurrent dispatch, resume, and operator commands.
It also defends against a contained agent, which cannot write outside its mount.
A host agent runs with the operator's privileges and can also edit the ledger.
A host agent is outside this boundary. Containment is the defense for that case.

Immutable audit evidence records device, inode, available birth identity, nonce, and token before launch.
The existing `checkout_id` field stores the complete binding as canonical JSON.
Resume, launch, base recording, and publication compare live kernel identity with that evidence.
The checkout admission lease holds a directory descriptor for the run.
Each boundary compares `os.fstat(fd)` and `os.stat(path)` with the persisted evidence.
The run closes the descriptor when it releases its checkout lease, including failure paths.
The sidecar and `.git/kew-generation` provide consistency checks, never identity evidence.
No agent-writable file authorizes checkout identity.

## Covered post-run publication case

Canonical publication closes the former host-Git boundary gap. The default is
`[publication] publish = "canonical"`. Host Git operates only in a fresh
host-owned bare repository. Kew never asks Git to inspect the agent checkout.
Checkout hooks, filters, and `core.fsmonitor` cannot execute during publication.
No Git process rooted in the agent checkout receives the publication credential.
Host Git in the canonical repository and in the operator's project checkout can inherit it.

These criteria cover post-run publication for dispatch and review remediation.
They also cover resolve and CI fix flows.
First-class loops are inside #1218's covered post-run publication case under canonical publication.
The host publishes each completed iteration through the same publication seams as dispatch.
Loops commit inside their own checkouts only under the admitted `publish = "checkout"` mode.
That mode requires `[sandbox] dangerously_skip_containment = true`.

| #1218 criterion | Mechanism | Owning test |
|---|---|---|
| Origin credentials never reach the agent checkout. | Userinfo is stripped from the remote URL kew writes into every agent-visible checkout. | `tests/test_worktree.py::test_agent_visible_origin_strips_credentials`; `tests/test_worktree.py::test_clone_local_checkout_strips_origin_credentials` |
| Checkout `remote.origin.url` and `remote.origin.pushurl` cannot redirect publication. | Operator configuration selects the remote. A fresh host-owned repository performs the push. | `tests/test_publication_canonical.py::test_checkout_remote_metadata_cannot_redirect_canonical_push` |
| Checkout `pre-push`, `pre-commit`, and redirected hooks never execute. | Canonical publication never runs Git against the agent checkout. | `tests/test_publication_canonical.py::test_checkout_hook_and_fsmonitor_never_run`; `tests/test_host_git_boundary.py::test_canonical_worker_paths_never_run_checkout_hooks` |
| Checkout changes to `remote.origin.url` and `remote.origin.pushurl` never reach host publication. | `publication_canonical.publish_checkout` passes the operator origin to `publication_repository.provision`. The fresh bare repository never reads checkout remotes. | `tests/test_host_git_boundary.py::test_no_host_git_resolves_inside_an_agent_path`; `tests/test_publication_canonical.py::test_checkout_remote_metadata_cannot_redirect_canonical_push` |
| Checkout publication needs explicit host-risk admission. | The selector requires `[sandbox] dangerously_skip_containment = true`. | `tests/test_publication.py::test_checkout_publication_requires_explicit_uncontained_admission`; `tests/test_dispatch_core.py::TestGitPushBranchRetry::test_checkout_escape_refuses_before_publication_without_admission` |

Two limits remain. The host walker reads agent files as data. A hostile file can
consume bounded reading work, but it cannot become Git configuration or code.
The temporary `publish = "checkout"` escape hatch restores the former risk.
It requires `[sandbox] dangerously_skip_containment = true` and records
`uncontained-publication` posture. Its commit is built with hooks, fsmonitor,
filters and signing off, and the publication filter runs over every pushed change
(#2634). Other Git it runs in the checkout still reads the checkout's configuration.
This setting admits the same host risk as uncontained agent execution.
`[sandbox] enabled = true` requires containment and rejects the dangerous flag.
Therefore, required containment disables host-checkout publication by design.

`publication_job.PUBLICATION_JOBS_ACTIVE` remains `False`. That switch controls
the unattended lifecycle. It does not control synchronous canonical publication.

With `enabled = true`, review remediation and conflict resolution fail closed when containment prerequisites are missing.
Missing prerequisites include the runtime, runner image mapping, and declared credential in the environment.
`allow_uncontained_flows = true` explicitly permits their host fallback with `admission = "policy"`.
The setting defaults to unset; unset and `false` permit no exception.
Auto fallback records `admission = "prerequisite_gap"`; explicit `enabled = false` retains `admission = "policy"`.
CI-fix retains its stricter dispatch gate and exit code 2 (could not run), including when this setting is `true`.
Review and resolve containment refusals return exit code 2 (could not run).

### Official Codex boundary

On the `docker` and `podman` runtimes the official Codex runner uses
Codex-native `workspace-write` on the trusted host rather than kew's external
container; on `[sandbox] runtime = "openshell"` a Codex dispatch is contained
like any other and `workspace-write` becomes the inner of two boundaries
([per-runtime posture](runners/codex.md#posture-per-runtime)). On the host lane
kew removes provider, GitHub, and AWS credentials from the process environment
while retaining `HOME` for official CLI login, protects `.git`, and publishes
through the canonical repository. The repo-supplied-config admission — project
`.codex/config.toml`, unknown entries, symlinks and special files fail closed —
is asked on both lanes, because the contained lane uploads the same tree. These
controls reduce reach but do not turn the trusted runner CLI or host into an
adversarial boundary.

`workspace-write` restricts writes and command network access, not reads.
The agent can read any file the host user can read, including credential files such as `~/.codex/auth.json`.
It can copy what it reads into the checkout.
Before the push, the [publication output scan](security.md#publication-output-scan) refuses output that contains a credential value kew holds, a secret value from the host's Codex login file (`$CODEX_HOME/auth.json`, else `~/.codex/auth.json`), or a known key shape, including OpenAI keys.
A token kew does not read, such as a Codex login kept in the OS keychain instead of that file or another tool's credential file, is caught only by its shape.
Review a Codex pull request before you merge it.

## Recommended hardening

- Run kew and its agents in an **isolated environment** (container / VM / dedicated user).
  `[sandbox] network = "allowlist"` joins the `kew-allowlist` network.
  kew does not create or modify this network during dispatch.
  `kew sandbox network`, which `kew init` offers to run, creates it as a plain bridge when it is absent, and installs no rules on it. The remote-host recipe does the same.
  Provision it with rules for approved inference endpoints and GitHub.
  A plain bridge has unrestricted egress and is not an allowlist.
- Use **scoped, rotatable credentials** (a fine-grained `gh` token, a project-scoped API
  key) — never long-lived org-wide secrets in the agent's reach.
- Keep `--dangerously-skip-permissions` for sandboxed contexts only; the security hook is
  a backstop, not a substitute for isolation.
- Add project-specific denials via `[security] blocked_prefix_patterns` /
  `blocked_exact_patterns` in `kew.toml`.
- Review the governance record with `kew audit tail` / `kew audit export`, and check its
  integrity with `kew audit verify`. Anchor the chain head (`kew audit anchor` / the
  anchoring workflow) and push the anchor file off the host, so a later edit to anchored
  events is detectable to a third party — within the limits in [docs/audit.md](audit.md) (kew is not an HSM; a host actor
  with the anchor token can still rewrite and re-anchor).

## Scope and evolution

This threat model reflects kew's current posture. M3 (tamper-evidence) has shipped — the
hash chain, `kew audit verify`, policy binding, and anchoring moved the audit row
from "auditable" to "tamper-evident within stated limits." Verifiable egress control
is a direction on the [roadmap](roadmap.md), not a current claim; it ships before it is
marketed.
