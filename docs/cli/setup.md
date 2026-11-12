# kew setup

One-time setup: creates GitHub labels, generates the security hook, writes the kew block into `AGENTS.md`, and creates log directories.

```text
Usage: kew setup [OPTIONS]

Options:
  --with-skills
  --json         Print one JSON document of what setup did on stdout.
```

```bash
kew setup
kew setup --with-skills   # also install every shipped skill + hook
kew setup --json          # machine-readable report on stdout
```

This creates:

- **24 GitHub labels** with the default `[labels]` config: 17 status, 4 priority and 3 complexity labels
- **`.claude/hooks/security-guard.sh`** — blocks destructive commands even with `--dangerously-skip-permissions`
- **`.claude/settings.json`** — creates or updates settings, merging the security hook into any existing `PreToolUse` handlers
- **`AGENTS.md`** — a short kew block telling agents in this repo what kew is and what to run first (see below)
- **`logs/dispatch/`** directory

## `AGENTS.md`

Codex and other agents read `AGENTS.md`, so setup writes a short block there: what kew is, the three commands an agent runs first (`kew doctor --json`, `kew init --yes`, `kew schema`), that approvals, `ready` labels and merges belong to a human, and a pointer to the [`operate-kew` skill](../skills.md#operate-kew).

kew owns only the text between its two marker lines:

```markdown
<!-- kew:begin -->
...
<!-- kew:end -->
```

- No `AGENTS.md`: setup creates it holding only the block.
- An `AGENTS.md` without the markers: the block is appended after a blank line.
- An `AGENTS.md` with the markers: only the text between them, markers included, is replaced. Every byte outside them, line endings included, stays as it was, so a re-run is a no-op.
- Markers that do not pair up (one missing, out of order, or more than one block): the file is left untouched and setup warns; it counts as a setup finding, like a skipped artifact.

## `--with-skills`

Also installs every artifact in kew's [skills catalog](../skills.md) (the `plan-to-issues`, `playbook-skill`, `verify-parity`, `handoff` and `operate-kew` skills, the `adversarial-reviewer` prompt, plus the `security-guard` hook it installs anyway) — equivalent to running `kew skills install --all` right after setup. If an artifact is already installed and locally modified, it's skipped with a warning rather than overwritten; use `kew skills update --force` to overwrite deliberately.

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
directories. `artifacts` maps each `--with-skills` artifact to `installed`,
`updated`, `unchanged` or `skipped` (locally modified). `agents_md.action` is
`created`, `appended`, `updated`, `unchanged`, or `skipped` when the markers do
not pair up. `ok` is `false` when a label failed, an artifact was skipped or
`AGENTS.md` was skipped, and the document is still printed. A
refusal before setup runs (no repo, label preflight unavailable) prints no
document. The exit status is the same with or without `--json` — see
[Exit codes](exit-codes.md).
