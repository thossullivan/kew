---
description: kew runs a coding agent on each GitHub issue you label ready, in its own clone under a budget, and opens a pull request for you to review.
hide:
  - toc
---

# kew

**Bottom line up front:** kew lets coding agents do the work while you keep the
judgment. You label GitHub issues `ready`. kew runs a coding agent on each one
in its own clone, under a budget, and opens a pull request for you to review.
Nothing merges on the word of the agent that wrote it.

Agents can now write more code than a small team can check and maintain. I call
that limit the maintenance ceiling. The model is the small box. The rest of the
system decides whether that code is worth keeping: who checks the work, who owns
the merge, and what happens when a run fails overnight.

kew is my answer to that rest of the system: an open-source control plane for autonomous
coding agents. It treats every agent as an untrusted author: what the agent
writes is input to check, never proof that the change works. **A change never
defines its own proof.** [How a change is proven](proof.md) lists what an agent
can write, what kew reads instead, and the code, tests and limits behind each
rule.

I build kew with kew. Agents opened 1,156 of the 1,615 pull requests merged into
kew between March and 10 November 2026 (bots excluded), and every merge went
through my GitHub account.

Your backlog has 30 issues. You have time for 3. Label the well-scoped ones
`ready`, and they get worked in parallel overnight. The scoping and the review
stay yours.

## Where to start

- [Getting started](getting-started.md): install kew and run your first dispatch.
- [How a change is proven](proof.md): the rules and where they apply.
- [Security](security.md) and the [threat model](threat-model.md): what each
  runner is contained by, and what kew does not defend.
- [CLI reference](cli/index.md): every command and its exit codes.
- [Philosophy](philosophy.md): why kew hands you a pull request, not a merge.

See it running: open the [live demo](https://demo.kew.dev/), or read
[what the demo is](web/demo.md) first.
