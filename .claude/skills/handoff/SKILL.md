---
name: handoff
description: Use when ending a session with in-flight work another session, machine, or person must pick up — uncommitted changes, unverified drafts, or multi-step work pausing midway. Writes a handoff doc that makes cold pickup safe.
---

# Write a Handoff Doc

A handoff doc lets a cold reader (human or agent) pick up in-flight work
without re-deriving context or trusting unverified code. Write it to
`<internal-docs>/handoff/YYYY-MM-DD-<topic>.md`.

## Internal docs directory

This skill writes internal working material, which never belongs in a project's
published `docs/` tree — that tree is built and published. Resolve
`<internal-docs>` once, before writing anything:

1. Use the internal docs directory the project declares in its `CLAUDE.md` or
   `AGENTS.md` (look for "internal docs directory").
2. Otherwise use the default, `.kew/docs/` — create it if missing, and make sure
   the project gitignores it.

## Required sections, in order

1. **Header: TL;DR + trust level.** One paragraph: what exists, what state
   it's in, what the first job is. Then a provenance line: who/what wrote
   the code, which gates it has and has NOT passed. "Treat it as a reviewed
   draft, not a passing build" beats discovering that later.
2. **Current git state.** Branch, base, committed vs staged vs untracked.
   Name any pre-existing dirty files the pickup must NOT touch.
3. **Files in the change set.** Table: path | state (new/modified) | what it
   is, one line each.
4. **Immediate actions, in order.** The exact commands to verify the work,
   copy-pasteable, in dependency order (install → format → lint → types →
   tests → live smoke). The pickup's first job is running these, not
   extending the work.
5. **Least-tested seams.** Numbered list of the integration points you could
   least validate, most-risky first, each with how to verify it.
6. **Invariants to preserve.** The rules the work must keep honoring, so a
   pickup doesn't "fix" something load-bearing.
7. **How to pick this up.** One paragraph a coordinator can paste to an
   agent: read this doc, run the checklist, fix fallout, and the explicit
   scope boundary ("verify + commit, not extend").

## Rules

- Never claim a gate passed that you didn't run. List unrun gates
  explicitly — the doc's value is calibrated trust.
- Convert every relative date ("yesterday") to an absolute one.
- If work is unverified because your environment couldn't run it, say
  exactly which environment can.
