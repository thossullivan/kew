# Changelog

kew follows [Semantic Versioning](https://semver.org/) from 1.0.0.

<!-- version list -->

## 1.1.0

### Upgrade note

A credentialed container now refuses to start on a network that kew cannot
show is closed. A network is closed when `[sandbox] network = "none"`, or when
`<runtime> network inspect` shows `Internal` true or a `null` or `none`
driver. The `kew-allowlist` network that `kew sandbox network` creates is a
plain bridge. On that network every credentialed container launch refuses
after the upgrade: dispatch on any runner, the adversarial reviewer, review
remediation, `kew resolve`, `kew ci-fix` and epic planning. `kew doctor`
fails `allowlist-network`. Clear the refusal in one of three ways:

- Use the Switchyard sidecar route.
- Make the network internal, where inference does not need the direct route.
- Accept the open egress with `[sandbox] acknowledge_open_egress = true`.
  Each run then warns about the open egress, and `kew doctor` passes
  `allowlist-network` with that warning.

The adversarial reviewer never uses the sidecar, and an internal network
blocks its inference, so for the reviewer only the acknowledgement works. A
host firewall is invisible to kew, so it does not clear the refusal.

The default agent image tag is now `kew-agent:docker-236e8d2ed49d`. kew never
pulls images, so run `kew sandbox build` after the upgrade, and
`kew sandbox build --runner <runner>` for each mapped Codex or OpenCode
runner.

### Changed

- Every credentialed container route checks egress before launch. A dispatch
  placed on a remote instance answers to that instance's network, and a
  refused placed dispatch lands `refused`: a queue item is marked failed and
  an epic node is held. The refusal token is `open_egress_unacknowledged`.
  Remediation's `auto` mode refuses instead of falling back to the host.
- `kew doctor` checks every container route, including the adversarial
  reviewer, and skips host and sidecar routes.
- `kew init` writes a `kew.toml` that turns run reports on. A hand-written
  `kew.toml` still keeps them off by default.
- `kew init` and `kew setup` install the security-guard hook and write the
  `AGENTS.md` block only when the operator passes `--guard-hook` or
  `--agents-md`, or says yes when `kew init` asks.
- The `kew-switchyard` image builds from public upstream Switchyard by commit
  SHA. That build reports version `0.3.0`, so a sidecar setup sets
  `expected_version = "0.3.0"`. kew no longer
  records a served tier. The benchmark counts call attempts, failed fallbacks
  included, and classifier completeness counts routing decisions.
- Every agent image moves to the current debian base.

### Added

- `[sandbox] acknowledge_open_egress` (default `false`).
- `[publication] pr_title_style`: `"sentence"` (the default) or
  `"conventional"`, which titles PRs and squash commits as
  `type(scope): summary`.

### Fixed

- `kew kill` and the console's stop action confirm a contained run's stop on
  the configured container runtime.
- A relative `logging.dir` in a linked worktree opens the main checkout's
  ledger, so removing the worktree no longer causes a wrong-ledger refusal. A
  refusal for a missing registered database says how to recover.
- `kew publication attest` on a branch with no kew run says so and points to
  `kew finalize`.
- Contained verification runs give tests an executable `TMPDIR` while `/tmp`
  stays `noexec`, so the pre-review static gate checks PRs again.
- The upgrade hint kew prints keeps the `serve` extra, so an upgrade keeps the
  web console. `kew serve` without the extra prints the `uv tool install`
  command.

## 1.0.0

First public release.

- kew watches a GitHub issue queue, takes `ready`-labeled issues in priority
  order, gives each one a standalone clone on its own `agent/issue-<N>` branch,
  runs the configured runner and opens a pull request.
- Dispatch is contained by default (`[sandbox] enabled = "auto"`): the agent
  runs in a container or inside a runner-native boundary, and kew refuses the
  run when neither is available. No GitHub token enters the container; the
  host publishes the agent's work.
- A change never defines its own proof: the merge verify (`kew verify`) runs
  the base branch's copy of each test file a pull request changed against the
  pull request's source. The merge verify is wired to kew's own repository
  layout today. Under governed review, only the reviewer's accepted lines reach
  **Behavior changes**.
- Optional governed adversarial review (`[review] adversarial = true`, off by
  default) runs a fresh-context `claude-code` reviewer on epic nodes that
  `kew epic` or `kew drive` advances, and on one pull request with
  `kew review --governed <PR>`. Each verdict is bound to the head commit it
  reviewed.
- Governance sits outside the runner: a hash-chained audit log
  (`kew audit verify`), a fleet kill switch (`kew stop` / `kew resume`),
  opt-in approval gates that hold a dispatch above a cost threshold for
  sign-off, and `kew report`, which ties spend at list price to merged pull
  requests and failed runs.
- `kew epic` drives an `epic:<name>`-labeled dependency graph of issues, one
  issue per node.
- For Claude Code, a `PreToolUse` security hook denies known-destructive shell
  commands. It is defense in depth, not the isolation boundary.
