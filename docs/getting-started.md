---
description: One path from installing kew to its first dispatch, run from inside the git repository you want kew to work on.
---

# Getting Started

**Bottom line up front:** one path takes you from install to your first
dispatch in five steps. Run every step from inside the git repository you want
kew to work on; no kew source checkout is needed. You check the setup before
anything is spent: `kew doctor` names each failing check and, where one
command fixes it, prints that command as `fix:`; and `kew run --dry-run` shows which issue would go first.

I made the `ready` label the queue on purpose. `kew run` takes only the issues
labeled `ready`, so the first decision, what is worth an agent's time, stays
with whoever applies the label: you.

Driving kew from a script or another agent? [Agents](agents.md) covers the
same path without a terminal: `kew init --yes`, the `kew doctor --json` loop,
`--json` output and exit codes.

## Prerequisites

| Tool | Purpose | Install |
|------|---------|---------|
| [uv](https://docs.astral.sh/uv/) | Install the kew CLI | `brew install uv` |
| Python 3.13 | Runtime | `brew install python@3.13` |
| [git](https://git-scm.com) | Version control | `brew install git` |
| [gh](https://cli.github.com), logged in | GitHub API: kew reads issues and opens PRs through it | `brew install gh`, then `gh auth login` |
| An agent runtime | Where a contained agent runs (not needed on a `codex` route) | See below |
| [claude](https://docs.anthropic.com/en/docs/claude-code) | Default AI coding agent | `npm i -g @anthropic-ai/claude-code` |
| [codex](https://developers.openai.com/codex/cli/) | Optional official Codex coding agent | Install the CLI, then run `codex login` |
| `ANTHROPIC_API_KEY` | Anthropic API authentication for `claude-code` | [docs](authentication.md) |

The agent runtime is where kew runs the coding agent, not a way to install kew.
kew supports these:

- **Docker** (Docker Desktop) — `[sandbox] runtime = "docker"`.
- **OrbStack** — provides the `docker` CLI, so kew uses it as `docker`.
- **Podman** — `[sandbox] runtime = "podman"`.
- **OpenShell** — `[sandbox] runtime = "openshell"`; chosen only with
  `kew init --runtime openshell`. See [OpenShell](openshell.md).

`kew init` detects `docker`, then `podman`, on `PATH`.

## Install and first dispatch

1. **Install the CLI.**

    ```bash
    uv tool install "kew-cli[serve]"
    ```

    That gives you the `kew` command and, through the `serve` extra, the web
    console. Or run `npx create-kew`: it installs the same package with uv,
    then runs `kew init` (step 2). It needs Node.js 18 or later and uv.

    Install kew as a tool. Do not add it to your application's dependency
    manifest.

2. **Write the configuration.**

    ```bash
    kew init
    ```

    [`kew init`](cli/init.md) detects the repository, runner, CI and format
    commands and the agent runtime, and asks only for what it could not
    detect. It writes the smallest valid `kew.toml`, then runs
    [`kew setup`](cli/setup.md), which creates the labels and the security
    hooks. On a terminal it offers to build the agent image
    (`kew sandbox build`) and create the `kew-allowlist` network
    (`kew sandbox network`) when those checks fail. It ends by printing the
    `kew init --yes ...` command that writes the same file.

3. **Check that dispatch is ready.**

    ```bash
    kew doctor
    ```

    [`kew doctor`](cli/doctor.md) checks the runtime, the runner image, the
    allowlist network, the runner credential and the `gh` login. A
    failing check prints a `fix:` command where one command fixes it. Run it, then run `kew doctor`
    again until it exits `0`.

4. **Preview the first dispatch.**

    ```bash
    kew run --dry-run
    ```

    kew's queue is the `ready` label: nothing dispatches until you add it to
    an issue, for example with `gh issue edit <n> --add-label ready`. The
    preview shows which issue kew would pick, without running anything.

5. **Dispatch.**

    ```bash
    kew run
    ```

    Or name one issue with [`kew dispatch <issue>`](cli/dispatch.md).

To change a setting later, re-run `kew init --force` with the flags you want;
see [Configuration](configuration.md) for every key.

!!! warning "Before the first dispatch"

    Review the [runner capability manifest](runners/index.md#runner-capability-manifest) before choosing your first route.

    Dispatch is contained by default and kew **refuses** to dispatch when no
    container runtime is found. The default `claude-code` route also needs its
    agent image and the allowlist network; `kew doctor` exits `1` and names the
    `fix:` command for each until both exist.

    [`kew sandbox network`](cli/sandbox.md#kew-sandbox-network) creates
    `kew-allowlist` as a plain bridge on your configured runtime and leaves an
    existing one untouched. kew installs no firewall rules on it: a plain
    bridge has **unrestricted egress** while the container holds your provider
    key, and `kew doctor` says so. See
    [Security](security.md#sandboxed-dispatch-credential-posture) for the
    egress posture. Ordinary contained dispatches warn about open egress.
    Review, resolve, ci-fix, and epic plan do not warn.

    First-class loops (`kew dispatch <issue> --loop`) are not contained: kew
    has no contained loop launch, so a loop's agent runs on the host. On the
    default `claude-code` route a loop refuses before any checkout or agent
    work unless `[sandbox] dangerously_skip_containment = true` is set. See
    [`kew loop`](cli/loop.md) for the refusal and its evidence.

    A `codex` route needs none of this on the `docker` and `podman` runtimes:
    the official Codex CLI sandboxes itself and kew launches no container. On
    `[sandbox] runtime = "openshell"` kew contains a codex *dispatch* itself
    ([details](runners/codex.md#posture-per-runtime)); a codex *loop* still
    runs under the CLI's own boundary, because no runtime has a contained loop
    launch. To run an agent on the host with no boundary, set
    `[sandbox] enabled = false` **and** `dangerously_skip_containment = true`
    (see [Security](security.md#sandboxed-dispatch-credential-posture)).

## What happens next

1. kew picks the highest-priority `ready` issue from your GitHub queue.
2. It creates a standalone clone with its own `.git` on a branch like `agent/issue-42`.
3. If an [approval gate](configuration.md#approval) is configured and the estimated cost meets your threshold, the issue is parked under `needs-approval` before the agent runs. Release it with `kew approve N`, the TUI `A` key, or the web inbox (`kew serve`).
4. Your configured runner works the issue inside your CI, budget and security guardrails.
5. On success, kew opens a PR and labels it `pr-pending-review`.
6. You review the PR like any other. The merge is your call.

See [How It Works](how-it-works.md) for the full dispatch loop.

A pull request is only as good as the issue behind it. Before you label an
issue `ready`, I would ask:

- Does the body say what done looks like? When it has an acceptance section,
  kew records it and puts it in the agent's prompt.
- Is it small enough that you can review the pull request in one sitting?
- Does it wait on another open issue that should land first?
- Who will review the pull request when it opens?
