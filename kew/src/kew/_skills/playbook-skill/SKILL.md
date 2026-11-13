---
name: playbook-skill
description: Use when a subsystem keeps breaking under agent (or human) edits and you want to encode its non-obvious invariants as a reusable checklist skill — creates an incident-mined playbook SKILL.md for that subsystem
---

# Write a Playbook Skill for a Fragile Subsystem

A playbook skill replaces judgment with a checklist: it encodes a subsystem's
non-obvious invariants so a cheaper model (or a hurried human) can change the
subsystem safely. This skill drafts one for YOUR repo.

## Inputs

Ask for (or infer): the subsystem (a file, module, or concern like "auth
routes"), and where its history lives (this repo's git log, CI runs, issues).

## Procedure

1. **Mine incidents.** For the subsystem's files:
   - `git log --oneline --follow -- <paths>` — find fix/revert commits.
   - `gh issue list --search "<subsystem> in:title" --state all` and
     `gh pr list --search "<subsystem>" --state merged` — find incident issues.
   - CI history if available: which jobs went red after touching these files?
2. **Extract invariants.** For each incident, write ONE invariant: the rule
   that, had it been followed, would have prevented it. Anchor every invariant
   to `file:line` in the current tree (verify the line still exists).
3. **Find the proving commands.** Identify the exact test command(s) that
   exercise the subsystem, and any subsystem-specific gates (lint rules,
   schema checks). Copy them verbatim — no paraphrasing.
4. **Draft the skill** at `.claude/skills/<subsystem>-change/SKILL.md`:

````
---
name: <subsystem>-change
description: Use when touching <subsystem> — <files/symbols that trigger it>
---
# <Subsystem> Change

## Invariants (each traces to an incident or a test)
- <invariant> (<file:line>; incident: <issue/PR #>)

## Checklist
- [ ] Write the failing test first (<canonical test file to copy from>)
- [ ] <subsystem-specific steps>

## Acceptance
Run: <one exact command whose exit 0 means the change is safe>
````

## Quality bar (enforce before saving)

- **Every invariant must trace to an incident or an existing test.** An
  invariant you can't source is an opinion — cut it.
- Every file:line anchor verified against the current tree.
- Exactly one copy-pasteable acceptance command.
- One page maximum. If it's longer, the subsystem needs decomposition, not
  a longer checklist.
