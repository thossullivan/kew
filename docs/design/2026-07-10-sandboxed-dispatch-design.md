# Sandboxed Dispatch — Design

**Date:** 2026-07-10
**Status:** Approved (maintainer brainstorm, 2026-07-10)
**Supersedes:** the in-process guard approach to audit findings **F1** (Critical)
and **F6** for containment purposes (see #502). The generated `security-guard.sh`
hook is retained as a best-effort **second layer**, not the primary control.

## Problem

kew dispatches coding agents as **host subprocesses** in git worktrees under
`--dangerously-skip-permissions` (`subprocess_utils.py:157`, `cwd=worktree`,
killed via `os.killpg`). The only thing standing between a prompt-injected agent
and the host is the generated in-process bash guard (`security-guard.sh`).

An adversarial security review (2026-07-10, recorded on #502) **defeated that
guard with 8 runtime-proven bypass classes**. The audit's specific known shapes
are caught, but the detection *model* is unwinnable: prefix-anchored matching
(`command git push --force`, `(git push --force)`, `nice …` all slip), an
under-powered segment splitter (single `&`, subshells, `{ }` groups never become
checked segments), and a literal-substring `.claude` test (glob `.cla*` and
var-split `d=.cla;e=ude` both **overwrite the guard file itself → permanent
self-neutralization**). This is exactly what the audit named: *"a hook that
guards itself is circular."*

String-matching a shell command is the wrong layer. A prompt-injected agent that
bypasses the guard can today read/modify `~/.claude`, exfil ambient credentials,
touch sibling repos, and corrupt the orchestrator's `dispatch.db`.

## Decision (made in brainstorm)

Move F1/F6 containment **up a layer** — to where the agent is deployed. Run each
dispatched agent inside a **hardened, ephemeral local container**, so a guard
bypass is *contained* rather than Critical.

- **Isolation model:** one ephemeral, hardened container per dispatch (rejected:
  microVM — stronger but slower/platform-specific; remote VM — defers to #363;
  host OS-sandbox — weaker/brittle). Containers give ~90% of the security at ~5%
  of the friction against the actual threat (a *prompt-injected agent*, not a
  kernel-escape attacker), and compose cleanly with a remote substrate later.
- **Runtime:** the `docker`-compatible CLI, auto-detected. Works with Docker
  Desktop, **OrbStack** (drop-in), and Podman with no
  runtime-specific code — just a configurable binary/context.
- **Mount policy (the primary control):** the container mounts **only** the
  dispatch's checkout. No host `~/.claude`, no `dispatch.db`, no SSH keys, no
  sibling repos, no host home. This removes the bulk of local blast radius.
- **Credential posture (least privilege + secret-zero where it matters):**
  - **GitHub credential never enters the container.** The agent commits inside;
    **kew pushes the branch and opens the PR from the host** using existing `gh`
    auth. The highest-value secret (push/org/supply-chain reach) stays out of the
    sandbox entirely — cheap, because push is already host-coordinated.
  - **Anthropic key:** a **short-TTL, scoped** key is injected into the container
    as an env var. Its blast radius is billing, not code; bounded by TTL + a spend
    cap. Full API brokering (a key-injecting proxy) is out of scope for v1 — a
    documented upgrade path, not a launch requirement.
  - **Egress: open.** Once the sandbox has no host FS and no GitHub token, there
    is almost nothing worth exfiltrating (only a short-TTL Anthropic key).
    Allowlisting egress is brittle (breaks `pip`/`npm`/`apt`) and low-value
    against an already-empty sandbox. Standard practice: *empty the sandbox,
    don't police the network.*
- **Defense in depth:** the in-process `security-guard.sh` still runs inside the
  container as a best-effort second layer. It is no longer load-bearing.
- **Opt-in, then default.** Ships behind config (`[sandbox] enabled`), default
  **off** for the first release; flip default **on** once proven and once a
  runtime is detected. The host-subprocess path remains for users without a
  container runtime.

## Threat model (what this defends against)

**In scope:** a prompt-injected or misbehaving coding agent running locally that
attempts to (a) modify/neutralize its own guard or kew's control plane, (b)
read/exfil host credentials or `~/.claude`, (c) damage host files outside the
work item, (d) reach sibling repos or `dispatch.db`.

**Explicitly NOT in scope (accepted):** container escape via a kernel 0-day
(needs microVM/remote — #363), a malicious *image* (we build/pin it), and
prompt injection itself (unpreventable; this bounds its blast radius). Exfil of
the short-TTL Anthropic key is accepted (bounded by TTL + spend cap).

## Architecture

### Execution flow (per dispatch)

1. **Host prepares a self-contained checkout** for the issue branch (see
   "Checkout & result return" — a standalone checkout, *not* a linked git
   worktree, so git operations work against a single mounted directory).
2. **Host launches the container:**
   `docker run --rm --name kew-<issue> --network <policy> --read-only-rootfs
   --cap-drop ALL --security-opt no-new-privileges --pids-limit N
   -v <checkout>:/work -w /work -e ANTHROPIC_API_KEY=<short-ttl>
   <kew-agent-image> <runner-cmd>`. Long-lived secrets, `~/.claude`, and
   `dispatch.db` are never passed in.
3. **Agent runs inside**, streaming to stdout as today. The existing
   `StreamEvent` parser consumes the stream unchanged (docker attaches stdout).
   The agent **commits** its work to the branch but does **not** push or open a
   PR (dispatch prompt updated — see Integration).
4. **On completion**, the container exits/is removed. The host reads the
   committed branch out of the checkout, **pushes to origin, and runs
   `gh pr create`** — the step the agent used to do itself.
5. **Kill/watchdog:** budget/timeout/fleet-kill target the container
   (`docker kill kew-<issue>`) instead of `os.killpg`.

### Components

- **`runtime` abstraction** — detect and wrap a `docker`-compatible CLI
  (`docker` on PATH by default; configurable binary + context). No
  OrbStack/Podman-specific code. Health-check at dispatch start; fall back to
  host-subprocess (or hard-fail) per config.
- **Agent image (`kew-agent`)** — a lean image with the coding-agent CLI (Claude
  Code), git, and common language toolchains. Distinct from today's CI
  `kew-runner` image (`docker/Dockerfile`), though it may share a base. Pinned by
  digest; built/published as part of release; buildable locally for air-gapped
  users. The plan resolves reuse-vs-new.
- **Checkout & result return** — v1 materializes a **standalone checkout** per
  dispatch (e.g. a local clone of the repo at the base commit with the issue
  branch created), mounted read-write as the *only* volume, so `git commit`
  works with a self-contained `.git` and the container never sees the main repo's
  full `.git` or other worktrees. The host then reads the branch out (the mount
  is host-visible) and pushes it. (Alternative — bind-mounting a linked worktree
  plus its backing gitdir — is rejected for exposing the whole repo `.git` and
  for host/container path fragility.)
- **Credential provisioning** — mint/short-scope the Anthropic key for the run
  (env var into the container); keep `gh`/GitHub auth on the host only.
- **Launch/kill wiring** — a container-launch path parallel to
  `subprocess_utils.py`'s Popen path, returning a handle whose stdout streams to
  the same parser and whose kill maps to `docker kill`. The budget watchdog and
  fleet kill switch (`os.killpg` today) get a container-aware branch.

### Config surface (`kew.toml`)

```toml
[sandbox]
enabled = false            # v1 default off; flip on once proven
runtime = "docker"         # docker-compatible CLI; OrbStack/Podman drop-in
image = "ghcr.io/thossullivan/kew-agent:<pinned-digest>"
network = "open"           # "open" (v1) | future: "allowlist"
on_missing_runtime = "host"  # "host" (fall back) | "fail"
# anthropic key TTL / spend cap sourcing documented separately
```

## Security posture rationale

The **mount policy** is the dominant control: an agent that cannot *see*
`~/.claude`, `dispatch.db`, or host credentials cannot harm them, regardless of
any guard bypass. On top of that: the **most dangerous credential (GitHub) never
enters the sandbox** (host brokers push/PR), the **Anthropic key is short-lived
and low-blast-radius**, and **egress stays open** because an empty sandbox has
nothing worth exfiltrating and network allowlists are brittle. This is the
least-privilege / secret-zero pattern applied where it pays, avoiding the
complexity of full API brokering and egress policing for marginal gain.

## Integration points (kew)

- `subprocess_utils.py` (`:150-184`) — add a container-launch sibling to the
  Popen path; same streamed-stdout contract to the parser.
- `dispatch_core.py` (`:640` `cwd=worktree`) — branch to container launch when
  `[sandbox] enabled`; prepare the standalone checkout; broker push + PR on
  completion.
- `_templates/dispatch.md.j2` — remove the agent-side `gh pr create` / `git push`
  instructions on the sandboxed path (host does it); keep `git commit`.
- Fleet kill switch / budget watchdog (`os.killpg` users) — container-aware kill
  (`docker kill`).
- `outcomes.py` / `sync_outcomes` — unchanged: it links the PR the host created,
  by branch (relates to launch-integrity I2/#507, which ensures branch is
  recorded).
- `config.py` — the `[sandbox]` block.
- Runner protocol (`runners/protocol.py`) — unchanged; the runner still parses a
  stream and classifies results. Sandboxing is a *launch/transport* concern, not
  a new runner.

## Error handling

- **No runtime available** → per `on_missing_runtime`: fall back to host
  subprocess (default) or hard-fail with a clear message. Never silently run
  unsandboxed when `enabled = true` and policy says `fail`.
- **Image missing/unpullable** → fail the dispatch with a build/pull hint; do not
  fall back to host silently.
- **Container crash / OOM / pids-limit** → surface as a dispatch failure with the
  container's exit code + last stream lines; retry per existing retry policy.
- **Commit present but push fails (host side)** → the work is safe in the
  checkout; mark `needs_human`, keep the checkout (mirrors #518's lesson: never
  discard committed work).
- **Agent produces no commit** → same classification rules as today, but the
  checkout is preserved for inspection.

## Testing strategy

- **Runtime abstraction:** unit-test detection + command construction with the
  `docker` CLI mocked (no live daemon in CI) — assert the hardening flags
  (`--cap-drop ALL`, `--read-only-rootfs`, `--security-opt no-new-privileges`,
  worktree-only `-v`, no `.claude`/`dispatch.db` mount, no `GH_TOKEN` in env).
- **Mount-policy assertion test:** given a dispatch config, the constructed
  `docker run` argv contains exactly the checkout mount and no forbidden paths.
- **Result-return:** host-brokered push/PR path tested with `gh`/git mocked.
- **Kill path:** watchdog/fleet-kill maps to `docker kill <name>`.
- **Live smoke (skip-marked, like `_REQUIRES_*`):** with a real runtime present,
  run one dispatch end-to-end in a container producing a commit the host pushes.
  Gated off in CI (no daemon).
- **Bypass-containment regression:** the audit/#502 bypass payloads, run *inside*
  the sandbox, cannot reach a host marker file outside the mount.

## Relationship to the launch gate (#439)

F1 is a Critical finding. Two honest options for launch, for the maintainer to decide:

1. **Sandbox is launch-gating** — F1 containment ships before go-live; this
   design joins the `#439` gate (likely a new `launch-sandbox` epic replacing the
   guard-patch nodes T1/T2).
2. **Document F1 as best-effort at launch, sandbox as fast-follow** — the threat
   model already states prompt injection is not prevented and the guard is
   heuristic; ship the honest doc + the merged T2 layer-2 guard, land sandbox
   immediately after.

Either way: **#502 stays open** until sandbox containment lands; **T2 (#515)
remains merged** as the second layer; the closed guard-patch **PR #519** is not
revived.

## Out of scope (deliberate)

- MicroVM and remote/cloud execution (remote is #363; this design's shape —
  checkout + scoped creds + ephemeral — is the same substrate later).
- Full Anthropic API brokering / key-injecting proxy (documented upgrade path).
- Egress allowlisting (revisit only if the sandbox ever holds high-value secrets).
- Rewriting the in-process guard's detection model — it stays as best-effort
  layer 2; we are not investing in the unwinnable arms race.
- Windows container support (revisit post-launch; macOS/Linux first).

## Success criteria

- A dispatched agent runs in an ephemeral container that mounts only its checkout;
  the constructed run command is asserted to exclude `~/.claude`, `dispatch.db`,
  and any GitHub token, and to carry the hardening flags.
- The agent commits inside; **kew pushes + opens the PR from the host**; the PR
  links back through `sync_outcomes` by branch.
- Budget/timeout/fleet-kill terminate the container.
- Works with Docker Desktop and OrbStack via the same `docker` CLI, no
  runtime-specific code.
- The #502 bypass payloads, executed inside the sandbox, cannot reach a host
  marker outside the mount.
- Cost-per-merged-PR stays within the current band (container overhead is
  seconds against minutes-long runs).
