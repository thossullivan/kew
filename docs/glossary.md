# Glossary

## agent PR

A pull request kew opened for an agent's work. Its branch is named `agent/issue-<number>` by default.

## audit chain

The log of governance events in the run store. Each row carries a hash of the row before it, and `kew audit verify` checks the chain. The chain shows that recorded rows were not edited. It does not show that every event was recorded. See [Audit](audit.md).

## broker

The host-side step that publishes an agent's work. On the contained route, kew reads the files the agent left, rebuilds the commit in a repository only the host touches, scans it, pushes the branch and opens the pull request. The agent holds no credential that can push.

## budget cap

A dollar limit on spend for one issue, one day or one week. kew refuses a dispatch that would exceed a cap. Dollars are API-equivalent list price, not money billed.

## checkout

A standalone clone that kew creates for one task. It has its own `.git` directory and branch.

## dispatch

One run of an agent on one issue. kew selects the issue, creates a checkout, builds the prompt, launches the runner and records the outcome.

## epic

A set of issues with dependencies between them, which kew drives as a graph. On GitHub an epic is a parent issue with sub-issues and blocked-by relations, or an `epic:<name>` label with `kew:depends` markers.

## governed review

A review of a pull request by a fresh agent that did not write it. kew reads the verdict from the reviewer's own final result event and binds it to the commit that was reviewed. See [How It Works](how-it-works.md).

## held-out tests

The base branch's copy of the test files a pull request changed. The merge verify runs them against the pull request's code, so an edited test cannot hide a regression. The agent can read these tests; it cannot edit the copy that runs. See [`kew verify`](cli/verify.md).

## loop

A supervised agent session started with `kew dispatch <issue> --loop`. It runs until a stop condition holds, such as CI green, and can be paused, resumed or reassigned.

## rescue

When a run hits its max-steps limit with coherent work, kew commits and pushes it and opens a flagged **draft** pull request instead of discarding it.

## runner

The coding agent that kew launches and supervises: Claude Code, Codex, OpenCode, pi, Hermes or Lemonade. See [Runners](runners/index.md).
