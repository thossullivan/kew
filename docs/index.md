---
description: kew runs a coding agent on each GitHub issue you label ready, in its own clone under a budget, and opens a pull request for you to review.
hide:
  - toc
---

# kew

kew is an open-source control plane for autonomous coding agents. It takes the
GitHub issues you label `ready`, runs a coding agent on each one in its own
clone under a budget, and opens a pull request for you to review.

kew treats the agent as an untrusted author. What the agent writes is input for
kew to check, not evidence that the change is correct: **a change never defines
its own proof.** [How a change is proven](proof.md) lists what an agent can
write, what kew reads instead, and the code, tests and limits behind each rule.

Your backlog has 30 issues. You have time for 3. Label the well-scoped ones
`ready`, and they get worked in parallel overnight.

## Where to start

- [Getting started](getting-started.md): install kew and run your first dispatch.
- [How a change is proven](proof.md): the rules and where they apply.
- [Security](security.md) and the [threat model](threat-model.md): what each
  runner is contained by, and what kew does not defend.
- [CLI reference](cli/index.md): every command and its exit codes.

See it running: the [demo console](web/demo.md).
