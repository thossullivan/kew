# kew

**The open-source control plane for autonomous coding agents.**

kew takes well-scoped issues from your GitHub queue, runs a coding agent on
each one in a standalone clone of your repository, and opens a pull request.
The agent can be Claude Code (`claude-code`), official Codex (`codex`),
`opencode`, `pi`, `hermes` or the experimental `lemonade` local-model loop.

kew treats the agent as an untrusted author. Budgets, the audit log and the kill
switch sit outside the runner, and a change never defines its own proof: kew
publishes the agent's work from the host, and its merge verify runs the base
branch's copy of each edited test against the new code.
[How kew decides a change is proven](https://kew.dev/docs/proof/).

## Install

```bash
uv tool install "kew-cli[serve]"
```

`kew-cli` installs the `kew` command. The unrelated `kew` package on PyPI is a different project.

The `serve` extra adds the web console. kew needs Python 3.13, so with pipx
use `pipx install --python python3.13 "kew-cli[serve]"`. A plain
`pipx install kew-cli` works when pipx itself runs on Python 3.13, and installs
the CLI without the console.
Install kew as a tool; do not add it to your application's dependencies.

Requires Python 3.13, the `gh` and `git` CLIs, and an agent CLI such as
`claude`. The default route also needs a container runtime: Docker Desktop,
OrbStack, Podman or OpenShell.

## Quickstart

Run these commands from inside the repository you want kew to work on:

```bash
kew init                                 # write kew.toml, then labels, hooks and checks
kew doctor                               # run each `fix:` it prints, then re-run
gh issue edit 42 --add-label ready       # mark a well-scoped issue dispatchable
kew run --dry-run                        # preview what kew would pick up
kew run                                  # dispatch an agent on the next ready issue
```

Dispatch is contained by default, and kew refuses to dispatch when no container
runtime is found. The default `claude-code` route needs `ANTHROPIC_API_KEY`;
see [Authentication](https://kew.dev/docs/authentication/).

A first contained dispatch needs the agent image and a network.
`kew sandbox build` builds the image. `kew sandbox network` creates
`kew-allowlist` as a plain bridge. kew installs no firewall rules on it, so the
container has unrestricted egress while it holds your provider key until you
add rules. `kew init` offers to run both.
See [Getting Started](https://kew.dev/docs/getting-started/) and
[Security](https://kew.dev/docs/security/).

## What kew checks

- **Host-owned publication.** On the contained route kew rebuilds the commit
  from the files the agent left and scans it before the push. The agent holds
  no credential that can push.
- **Merge verify.** The base branch's copy of each edited test runs against the
  pull request's code. It is wired to kew's own repository layout today.
- **Governed review** (opt-in). A fresh agent reviews the pull request, and the
  merge is bound to the commit it reviewed.
- **Budgets.** Per-issue, daily and weekly caps refuse a dispatch that would
  exceed them. Dollars are API-equivalent list price.
- **Kill switch.** `kew stop` halts the fleet; `kew resume` re-enables it.
- **Audit chain.** Governance events are hash-chained; `kew audit verify`
  checks the chain.
- **Spend report.** `kew report` ties spend to merged pull requests.

The [threat model](https://kew.dev/docs/threat-model/) lists what kew does not
defend.

## Links

- [Documentation](https://kew.dev/docs/)
- [Source and issues](https://github.com/thossullivan/kew)
- [Changelog](https://github.com/thossullivan/kew/blob/main/CHANGELOG.md)

Apache-2.0 licensed.
