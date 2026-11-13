---
name: plan-to-issues
description: Convert an implementation plan into sized, labeled GitHub issues for the dispatch queue (without "ready" label — human reviews first)
---

# Plan to Issues

Convert an implementation plan into sized, labeled GitHub issues for the `kew` queue. Each issue maps to a group of tasks from the plan and references the plan file directly — the dispatch agent reads the plan at execution time.

Issues are created **WITHOUT** the `ready` label. The human reviews each issue and labels `ready` manually before dispatch picks it up.

## Usage

In Claude Code, run:

```
/plan-to-issues .kew/docs/plans/my-feature.md
```

Or with no arguments, it will look for the most recently modified plan file.

## Internal docs directory

This skill writes internal working material, which never belongs in a project's
published `docs/` tree — that tree is built and published. Resolve
`<internal-docs>` once, before writing anything:

1. Use the internal docs directory the project declares in its `CLAUDE.md` or
   `AGENTS.md` (look for "internal docs directory").
2. Otherwise use the default, `.kew/docs/` — create it if missing, and make sure
   the project gitignores it.

Plans live in `<internal-docs>/plans/`.

## Input

Use whatever the user provides:

- **Arguments provided:** Use the referenced implementation plan directly.
- **No arguments:** Look in `<internal-docs>/plans/` (default `.kew/docs/plans/`) for the most recently modified implementation plan. Ask the user to confirm before proceeding.
- **Design doc reference:** If the user points to a design doc instead of a plan, suggest creating an implementation plan first.

Read the plan thoroughly before creating any issues.

## How Issues Map to Plans

Implementation plans group tasks into logical batches. Each batch becomes one GitHub issue:

```
Plan: 16 tasks across 3 groups
  Group 1 (Tasks 1-5)   → Issue #70 (medium, 5 tasks)
  Group 2 (Tasks 6-10)  → Issue #71 (medium, 5 tasks)
  Group 3 (Tasks 11-16) → Issue #72 (complex, 6 tasks → split into 2 issues)
    Issue #72: Tasks 11-13 (medium, 3 tasks)
    Issue #73: Tasks 14-16 (medium, 3 tasks)
```

**Cap at 5 tasks per issue.** If a group has >5 tasks, split into multiple issues. The dispatch agent delegates each task to a subagent, so >5 tasks risks orchestrator context pressure.

## Complexity from Task Count

| Tasks | Complexity | Label |
|-------|-----------|-------|
| 1-2   | Simple    | `simple` |
| 3-4   | Medium    | `medium` |
| 5     | Complex   | `complex` |

This scales with orchestrator overhead, not file count. Each task gets a fresh subagent.

## Dispatchability Gate (run per issue, BEFORE `gh issue create`)

Every issue must name, concretely:

- **(a) Target files** — real repo paths. Verify each exists (`ls`); mark new files "(new)".
- **(b) The test proving done-ness** — test file + test name(s) that fail before the work and pass after.
- **(c) An acceptance command** — one exact runnable command whose exit 0 means the issue is done.
- **(d) A complexity label consistent with file count** — `simple` ≤3 files, `medium` ≤6, `complex` ≤10. Over 10 files → re-split the plan group before creating anything. Task count assigns the initial label (see table above); file count only escalates — if the file count exceeds the label's ceiling, bump to the smallest label that covers it. Never de-escalate on file count. In epic/governed mode the planning agent enforces the re-split itself: a plan node exceeding 10 files becomes multiple issues (recorded in the summary table) — it is never carried through as one oversized issue. If a governed draft still slips an oversized node through, `kew epic plan` rejects it and re-dispatches the planning agent **once** with a re-split instruction naming the offending nodes; a still-oversized second draft hard-fails the plan and creates nothing.

An issue failing any check is fixed (or the plan group re-split) — never created as-is.

## Issue Body Template

The issue body references the plan file — no need to duplicate task specs.

```markdown
## Summary

[1-2 sentences: what this group of tasks delivers]

## Plan Reference

**Plan file:** `.kew/docs/plans/YYYY-MM-DD-feature-name.md`
**Tasks:** N-M (Task N: [title], Task N+1: [title], ..., Task M: [title])
**Branch:** `agent/issue-[number]` (created automatically by kew)

## Done When

**Target files:** `path/one.py`, `path/two.py` (new)
**Proving test(s):** `tests/test_x.py::test_specific_behavior`
**Acceptance command:** `uv run pytest tests/test_x.py -v`

## Execution

The dispatch agent should:
1. Read the plan file and extract Tasks N through M
2. Execute each task following TDD: failing test → implement → verify → commit
3. Run full verification before creating the PR

## Acceptance Criteria

- [ ] [Observable outcome these tasks produce] (`command that proves it`)
- [ ] [Another outcome, naming the real symbol or file] (`command that proves it`)
- [ ] [... 2 to 6 lines; no boilerplate]

## Dependencies

<!-- kew:depends N -->
<!-- Machine-readable: space-separated issue numbers of predecessors -->
<!-- Delete this block if no dependencies (Wave 1 issues) -->

## Context for the Agent

**Key files to understand:**
- `path/to/relevant/file.py` — [why this file matters]

**Patterns to follow:**
- [Reference existing code that demonstrates the pattern]
```

**Use real file paths.** Browse the codebase to find the actual files. Don't use placeholders.

**Write 2 to 6 item-specific acceptance criteria per issue**, one `- [ ]` line
each: an observable outcome plus, wherever possible, the exact command that
proves it. The criteria are what the finished work is judged against, so
boilerplate true of every issue is useless and `kew epic plan` rejects it —
"All tasks N-M from the plan are implemented", "All new and existing tests
pass", "CI gate passes", "Format check passes", "Code reviewed",
"Documentation updated", "No regressions". A node needing more than 6 criteria
is more than one node; re-split it.

## Label Assignment

Apply exactly three labels to every issue — one from each category:

**Priority** (how soon it should be picked up):
- `p0-critical` — Blocking other work or broken in production
- `p1-high` — Should be done this session/day
- `p2-medium` — Default. Important but not urgent
- `p3-low` — Backlog. Nice to have

**Complexity** (from task count, see table above):
- `simple` — 1-2 tasks
- `medium` — 3-4 tasks
- `complex` — 5 tasks

**Type** (what kind of change):
- `feature` — New capability
- `bug` — Something broken
- `refactor` — Restructuring without behavior change
- `test` — Test coverage improvement
- `docs` — Documentation only

**NEVER add the `ready` label.** The human reviews each issue and labels `ready` manually.

## Epic Mode (`kew epic run`)

If the issues will be driven as an epic DAG (`kew epic run <name>`) instead of the classic ready-label queue:

1. **Apply `epic:<name>` to every created issue.** Create the label first if it doesn't exist:
   `gh label create "epic:<name>" --color 5319E7 --description "kew epic membership" --force`
2. **Never add the `blocked` label.** The driver computes the dispatch frontier from `<!-- kew:depends -->` markers; a manual `blocked` label actively holds a node back from dispatch.
3. Still **never add `ready`** — the driver reserves nodes itself and doesn't use it.
4. The wave table (below) is informational in epic mode — the driver schedules, not the labels.
5. Finish with `kew epic validate <name>` (rejects cycles/dangling deps) and `kew epic graph <name>` (Mermaid render), then hand off: review the issues, then `kew epic run <name>`.

`kew epic plan <name> --from <plan>` runs this same judgment as a governed dispatch with these guardrails enforced by kew.

## Title Conventions

Write each title as a behaviour statement: one sentence saying what the
project does once the issue is done, with no conventional-commit prefix.
kew's own commit subjects follow the same rule:

- `Users sign in and keep a session (Tasks 1-4)`
- `The dashboard shows queue state from its own API (Tasks 5-8)`
- `The queue processor takes each item exactly once (Tasks 9-10)`

Keep titles under 70 characters. Include the task range for traceability.

## Summary Table

After creating all issues, print a summary table:

```
| #   | Title                                         | Priority  | Complexity | Tasks | Depends On | Wave |
|-----|-----------------------------------------------|-----------|------------|-------|------------|------|
| #70 | Users sign in through the auth API            | p1-high   | complex    | 1-5   | —          | 1    |
| #71 | The dashboard shows queue state               | p1-high   | medium     | 6-8   | #70        | 2    |
| #72 | The backend sends notifications               | p2-medium | medium     | 9-11  | #70        | 2    |
| #73 | Users set notification preferences            | p2-medium | medium     | 12-14 | #72        | 3    |
```

**Wave** = dispatch order. Same wave = can run in parallel. Wave N+1 depends on wave N.

## Rules

1. **Every issue references the plan file with exact task range.** No duplicating task specs into issues.
2. **Cap at 5 tasks per issue.** Split larger groups.
3. **Use real file paths** in the context section. Browse the codebase.
4. **Never add the `ready` label.** The human decides when to dispatch.
5. **Declare dependencies explicitly.** Later groups depend on earlier groups. Use `<!-- kew:depends N M -->` comments with predecessor issue numbers. In classic-queue mode only, add the `blocked` label to Wave 2+ issues so the selector skips them until predecessors complete. In epic mode, never add `blocked` (see Epic Mode).
6. **One concern per issue.** If a group spans backend + frontend, consider splitting.
7. **Include CI gates in acceptance criteria.** Every issue.
8. **Every issue passes the Dispatchability Gate.** Target files verified, proving test named, acceptance command runnable, complexity label consistent with file count. Fix, don't create.
