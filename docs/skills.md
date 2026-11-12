# Skills catalog

kew ships a set of governance artifacts — Claude Code skills plus the security-guard hook — as package data. They install into your project's `.claude/` with [`kew skills`](cli/skills.md), which tracks drift so updates don't clobber your local changes.

```bash
kew skills list           # see what's installed and its state
kew skills install --all  # install everything
```

## Lifecycle

- **Install:** `kew skills install <name>` or `--all` copies the artifact into `.claude/`. `kew setup --with-skills` does this as part of one-time setup; a plain `kew setup` prints the install tip instead.
- **Update:** `kew skills update` with no names converges everything — it refreshes every artifact that's `outdated` *and* installs every artifact that's `not-installed`. Pass names to scope it.
- **Diff:** `kew skills diff` shows what changed, artifact by artifact, before you decide to update.
- **`locally-modified`** means the installed copy doesn't match the shipped content or any prior shipped version — you (or an agent) edited it by hand. `install`/`update` refuse to touch it (exit 1) unless you pass `--force`, which overwrites it with the shipped version.

See [`kew skills`](cli/skills.md) for the full command reference and the state machine (`not-installed` / `current` / `outdated` / `locally-modified`).

## Where the skills write: the internal docs directory

`/plan-to-issues`, `/handoff` and the repo-local `/audit` skill all produce **internal working material** — plans, handoff notes, audit reports. That material must never land in a project's published `docs/` tree, which is what MkDocs builds and deploys; kew unpublished its own gh-pages mirror on 2026-08-06 after ~214 internal pages reached the public site that way.

So the skills write under an *internal docs directory*, which each skill resolves itself before writing anything:

1. The internal docs directory the project declares in its `CLAUDE.md` or `AGENTS.md` — search for the phrase "internal docs directory".
2. Otherwise the default, **`.kew/docs/`**, created on demand.

Each skill owns a subdirectory of it:

| Skill | Writes to |
|---|---|
| `/plan-to-issues` | `<internal-docs>/plans/` (reads plans from there too) |
| `/handoff` | `<internal-docs>/handoff/YYYY-MM-DD-<topic>.md` |
| `/audit` (repo-local) | `<internal-docs>/audit/YYYY-MM-DD-codebase-audit.md` |

Gitignore the directory — `.kew/docs/` if you take the default. This is a **skill-level convention, not a kew runtime setting**: no kew command reads it, there is no `kew.toml` key for it, and nothing enforces it at dispatch time. The enforcement that does exist is negative and lives in the docs gate: `scripts/check-docs.sh` fails if anything in the tree points into a deleted internal docs tree, and MkDocs' `exclude_docs` keeps internal material out of a build.

### Guard updates preserve your config

The `security-guard` hook is the one artifact that isn't a flat file compare: its drift state is computed by re-rendering the hook template with your **current** `kew.toml`, not by diffing bytes against a fixed shipped copy. That means custom `[security]` patterns —

```toml
[security]
blocked_prefix_patterns = ["kubectl delete"]
blocked_exact_patterns = ["DROP DATABASE"]
```

— survive `kew skills update security-guard`: the extras are re-applied from config on every render, not diffed away.

kew owns any file that still carries the `# managed-by: kew` header, hand-edits included: if you edit the hook script in place, the header stays intact, so drift detection classifies it as `outdated` and `kew skills update` silently overwrites your edits with the re-rendered version. `locally-modified` protection only kicks in once the header is removed or the file is replaced outright. For persistent customization, put your patterns in `kew.toml`'s `[security]` block instead of editing the hook body — those survive updates because they're re-applied on every render.

## Catalog

### `/plan-to-issues`

Converts an implementation plan into sized, labeled GitHub issues for the dispatch queue.

```bash
# In Claude Code:
/plan-to-issues .kew/docs/plans/my-feature.md
```

**What it does:**
- Reads an implementation plan and groups tasks into issues (max 5 tasks per issue)
- Assigns complexity labels based on task count (1-2 = simple, 3-4 = medium, 5 = complex)
- Creates issues with plan file references -- the dispatch agent reads the plan at execution time
- Declares dependencies between issue groups (wave ordering)
- **Never adds the `ready` label** -- the human reviews and labels manually
- **Epic mode:** when the issues will be driven by `kew epic run`, the skill applies `epic:<name>` to every issue and skips `blocked`-label bookkeeping (the driver computes the frontier from `kew:depends`; a manual `blocked` label would hold nodes back)

```bash
kew skills install plan-to-issues
```

**Typical workflow:**

```text
Write plan  ->  /plan-to-issues  ->  Review issues  ->  Label "ready"  ->  kew run
```

Epic workflow (dependency-driven, no manual `ready` labels):

```text
Write plan  ->  kew epic plan <name> --from <plan>  ->  Review issues  ->  kew epic run <name>
```

The skill works with any implementation plan, however it's written. If you use a brainstorming -> design spec -> implementation plan pipeline (e.g., [Superpowers](https://github.com/anomalyco/superpowers)), run `/plan-to-issues` after your plan is finalized. The full pipeline looks like:

```text
Brainstorm  ->  Design spec  ->  Implementation plan  ->  /plan-to-issues  ->  Review  ->  Dispatch
```

### `/playbook-skill`

A meta-skill: it drafts a new playbook skill for one of *your* fragile subsystems, mining your own git log, CI history, and issues for incidents.

**What it does:**
- Mines `git log`, merged PRs, and issues touching the subsystem's files for fix/revert incidents
- Extracts one invariant per incident, anchored to `file:line` in the current tree
- Identifies the exact proving command(s) for the subsystem
- Writes `.claude/skills/<subsystem>-change/SKILL.md`: invariants, a checklist, and exactly one copy-pasteable acceptance command
- Enforces its own quality bar: every invariant must trace to a real incident or test, not an opinion

```bash
# In Claude Code, after installing:
# "Write a playbook skill for the auth routes module"
```

```bash
kew skills install playbook-skill
```

### `/verify-parity`

Fixes the "local checks pass, CI is red" incident class by building a verify script that mirrors CI exactly, plus a regression test that fails the moment they drift.

**What it does:**
- Enumerates every gate step in your `pull_request`-triggered CI workflows (command + scope)
- Reconciles them against a local `scripts/verify.sh` (or creates one), in both directions
- Harvests past red-CI incidents into a `Trigger -> exact command` Trap Gates table for your agent-instructions file
- Writes a parity regression test that normalizes and diffs the verify script against the CI workflows, so new drift fails CI itself

```bash
# In Claude Code, after installing:
# "Build a verify/CI parity script for this repo"
```

```bash
kew skills install verify-parity
```

kew's own `scripts/verify.sh` + `tests/test_verify_parity.py` is the worked example this skill cites.

### `/handoff`

Writes a handoff doc for in-flight work that another session, machine, or person needs to pick up cold — uncommitted changes, unverified drafts, work paused mid-task.

**What it does:**
- Captures a TL;DR + provenance line (what's been verified vs. not) up front
- Records exact git state and a files-in-the-changeset table
- Lists the exact, copy-pasteable verification commands in dependency order
- Flags the least-tested integration seams, most-risky first
- States invariants the pickup must not break, and the scope boundary for the pickup session

```bash
# In Claude Code, at the end of a session:
# "Write a handoff doc for this work"
```

```bash
kew skills install handoff
```

Writes to `<internal-docs>/handoff/YYYY-MM-DD-<topic>.md` — see [the internal docs directory](#where-the-skills-write-the-internal-docs-directory).

### `operate-kew`

Teaches an agent to install, configure and drive kew in this repository: the `kew doctor --json` → fix → doctor loop, `kew init --yes` and its flags, `kew schema` for discovering commands instead of guessing them, kew's exit-code families, and which actions only a human takes (approvals, `ready` labels, merges). `kew setup` points agents at it from the kew block it writes into `AGENTS.md` — see [`kew setup`](cli/setup.md#agentsmd).

```bash
kew skills install operate-kew
```

### `adversarial-reviewer` (prompt, not a skill)

The rubric kew's adversarial reviewer runs under. Without a copy of your own, the reviewer runs the pinned copy kew ships, so the governed review works whether or not you install it. Installing it writes `.claude/kew/adversarial-reviewer.md`: once that file is committed on your base branch, the reviewer runs your copy instead, so you can tune the rubric through a reviewed pull request — see [Prompt profiles](prompts.md#replacing-a-base-prompt).

```bash
kew skills install adversarial-reviewer
```

### `security-guard` (hook, not a skill)

The `PreToolUse` guard installed by `kew setup` (and manageable via `kew skills` like any other artifact). It catches destructive and exfiltration-prone shell patterns and blocks agent writes to `.claude/` — **defense-in-depth, not a security boundary.** See [Security](security.md) for the full framing, the pattern table, and how it relates to sandboxed dispatch.

```bash
kew skills install security-guard    # equivalent to `kew setup`'s hook generation
kew skills update security-guard     # re-render from current kew.toml
```

Because the guard blocks agent writes to `.claude/`, installed skills and the hook itself are protected from being tampered with by the very agent they govern — a closed loop, not just a one-time install.
