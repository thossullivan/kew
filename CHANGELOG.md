# Changelog

kew follows [Semantic Versioning](https://semver.org/) from 1.0.0.

<!-- version list -->

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
