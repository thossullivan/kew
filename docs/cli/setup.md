# kew setup

`kew setup` does a repository's one-time setup: it creates GitHub labels and
log directories. On request it also installs the security-guard hook and writes
the kew block into `AGENTS.md`. [`kew init`](init.md) runs it for you.

```text
Usage: kew setup [OPTIONS]

Options:
  --with-skills  Also install kew's shipped agent skills.
  --json         Print one JSON document of what setup did on stdout.
  --guard-hook   Install the security-guard hook in .claude/settings.json; it
                 runs in every Claude session in the repo.
  --agents-md    Write kew's block into AGENTS.md; it tells every agent in the
                 repo never to approve, label or merge.
```

```bash
kew setup
kew setup --guard-hook --agents-md   # also the guard hook and the AGENTS.md block
kew setup --with-skills              # also install every shipped skill
kew setup --json                     # machine-readable report on stdout
```

This creates:

- **24 GitHub labels** with the default `[labels]` config: 17 status, 4 priority and 3 complexity labels
- **`logs/dispatch/`** directory

With `--guard-hook` it also writes:

- **`.claude/hooks/security-guard.sh`** — blocks destructive commands even with `--dangerously-skip-permissions`
- **`.claude/settings.json`** — creates or updates settings, merging the security hook into any existing `PreToolUse` handlers

With `--agents-md` it also writes:

- **`AGENTS.md`** — a short kew block telling agents in this repo what kew is and what to run first (see below)

Without either flag, setup touches neither `.claude/settings.json` nor
`AGENTS.md` and prints a one-line tip naming the flags.

## The security-guard hook

With `--guard-hook`, the hook is wired into the repository's `.claude/settings.json`, so it runs
before every Bash, Write and Edit call in **every** Claude Code session in the
repository, yours included, not only kew's agents. That is why it is opt-in.
Containment, not the hook, is kew's security boundary, and a dispatch runs
without the hook: its posture record then reports `command_guard` as `absent`
(see [Security](../security.md#sandboxed-dispatch-credential-posture)).
Re-running `kew setup --guard-hook` leaves an installed hook and its settings
entry as they are.

## `AGENTS.md`

Codex and other agents read `AGENTS.md`, so `--agents-md` writes a short block there: what kew is, the three commands an agent runs first (`kew doctor --json`, `kew init --yes`, `kew schema`), that approvals, `ready` labels and merges belong to a human, and a pointer to the [`operate-kew` skill](../skills.md#operate-kew).

kew owns only the text between its two marker lines:

```markdown
<!-- kew:begin -->
...
<!-- kew:end -->
```

The block tells **every** agent that reads `AGENTS.md` that approvals, `ready`
labels and merges are a human's, so leave it out in a repository where you gave
a session that authority.

- No `AGENTS.md`: setup creates it holding only the block.
- An `AGENTS.md` without the markers: the block is appended after a blank line.
- An `AGENTS.md` with the markers: only the text between them, markers included, is replaced. Every byte outside them, line endings included, stays as it was, so a re-run is a no-op.
- Markers that do not pair up (one missing, out of order, or more than one block): the file is left untouched and setup warns; it counts as a setup finding, like a skipped artifact.

## `--with-skills`

Also installs every artifact in kew's [skills catalog](../skills.md) (the `plan-to-issues`, `playbook-skill`, `verify-parity`, `handoff` and `operate-kew` skills and the `adversarial-reviewer` prompt). The `security-guard` hook is installed only with `--guard-hook` as well; with both flags this is equivalent to running `kew skills install --all` right after setup. If an artifact is already installed and locally modified, it's skipped with a warning rather than overwritten; use `kew skills update --force` to overwrite deliberately.

Without `--with-skills`, `kew setup` prints a one-line tip pointing at `kew skills install --all` instead of installing anything.

## `--json`

`--json` prints **one JSON document** on stdout once setup has run; the
progress lines stay on stderr.

```json
{
  "schema": 1,
  "repo": "owner/repo",
  "labels": {"created": ["ready", "in-progress"], "updated": [], "failed": []},
  "hook_path": "/path/to/repo/.claude/hooks/security-guard.sh",
  "settings_path": "/path/to/repo/.claude/settings.json",
  "guard_hook": "installed",
  "directories": {"logs": "/path/to/repo/logs/dispatch", "worktrees": "/path/to/worktrees"},
  "artifacts": {},
  "agents_md": {"path": "/path/to/repo/AGENTS.md", "action": "created"},
  "ok": true
}
```

`labels` names each label by what happened to it: `created`, `updated` (it
already existed, so its color and description were refreshed), or `failed`.
Setup is idempotent: a second run reports every label under `updated`, leaves
a hook kew already wrote as it is, and reports the same hook path and
directories. `hook_path` and `settings_path` are where the hook is or would be
written; `guard_hook` is `installed`, or `not requested` without
`--guard-hook`. `artifacts` maps each `--with-skills` artifact to `installed`,
`updated`, `unchanged` or `skipped` (locally modified). `agents_md.action` is
`created`, `appended`, `updated`, `unchanged`, `skipped` when the markers do
not pair up, or `not requested` without `--agents-md`. `ok` is `false` when a label failed, an artifact was skipped or
`AGENTS.md` was skipped, and the document is still printed. A
refusal before setup runs (no repo, label preflight unavailable) prints no
document. The exit status is the same with or without `--json` — see
[Exit codes](exit-codes.md).
