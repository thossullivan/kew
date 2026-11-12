---
name: operate-kew
description: Use when installing, configuring, diagnosing or operating kew in a repository — running kew doctor until it is clean, writing kew.toml with kew init, discovering commands with kew schema, reading kew's exit codes, and knowing which actions only a human may take (approvals, ready labels, merges).
---

# Operate kew

kew is a control plane for autonomous coding agents: it picks GitHub issues
labelled `ready`, dispatches an agent onto each one in a sandbox, and opens a
pull request for a human to review. This skill is how an agent installs,
configures and drives it. Every command below is real; check any other one
against `kew schema` before you run it.

## Start here

Run these three, in this order, before anything else:

```bash
kew doctor --json   # what is broken, and the command that fixes each item
kew init --yes      # only when the project has no kew.toml yet
kew schema          # every command, option and exit-code family, as JSON
```

## The doctor loop

`kew doctor --json` prints one JSON document on stdout. Work it to clean:

1. Run `kew doctor --json`.
2. Stop when `ok` is `true` (exit `0`).
3. Otherwise take each check whose `status` is `fail` or `warn`. Its `fix`
   field, when not `null`, is the command to run; its `hint` says what to do
   in prose when no single command fixes it.
4. Apply one fix, then go back to step 1. Re-run doctor after every fix: one
   fix can clear or reveal another check.

`skip` means a check could not run. It is never a pass — read its `summary`.
If a fix needs a credential, a package install on the host, or a decision only
the operator can make, stop and report the check `id`, `summary` and `hint` to
the human instead of guessing.

## Configure with kew init

`kew init` writes the smallest valid `kew.toml` from what it detects, then runs
`kew setup` and the doctor checks. It never overwrites an existing `kew.toml`
unless you pass `--force`. An agent has no terminal to answer prompts, so
always pass `--yes` and supply what detection cannot know:

```bash
kew init --yes
kew init --yes --runner codex --ci "uv run pytest" --format "uv run ruff format ."
kew init --yes --repo owner/name --runtime docker --budget-per-issue 25
```

Flags: `--runner`, `--model`, `--ci`, `--format`, `--runtime`, `--repo`,
`--budget-per-issue`, `--yes`, `--force`. A flag always wins over detection.

`kew setup` is the one-time repository setup `kew init` runs for you: GitHub
labels, the security hook, log directories, and the kew block in `AGENTS.md`.
`kew setup --json` reports what it did as one JSON document.

## Discover commands with kew schema

Do not guess a command or flag. `kew schema` prints the live command tree:

```bash
kew schema | jq -r '.commands[] | .command'
kew schema | jq -r '.commands[] | select(.json) | .command'
kew schema | jq '.commands[] | select(.command == "kew status") | .options'
```

Each entry carries `path`, `help`, `arguments`, `options`, `json` (whether it
takes `--json`; prefer it whenever it is `true`) and `exit_codes.family`.

## Exit codes

Every command follows one scheme — did it run, and what did it find:

| Code | Meaning | What to do |
| --- | --- | --- |
| `0` | Ran; nothing to act on | Continue |
| `1` | Ran; found something (findings, drift, a failed check) | Read the output and act on the findings |
| `2` | Did not run (missing prerequisite, bad config, query failure) | Fix the prerequisite; retrying unchanged will fail again |

The `exit_codes.family` in `kew schema` names the row of the per-command table
at <https://kew.dev/docs/cli/exit-codes/>.

## Everyday commands

```bash
kew status --json            # the queue, running work and recent runs
kew dispatch 123 --dry-run   # show what dispatching issue 123 would do
kew skills list --json       # which shipped skills are installed
kew skills install operate-kew
```

## What only the human does

kew gates these on a person on purpose. Never perform them, and never work
around them — ask the human and say exactly what is waiting:

- **Approvals.** `kew approve` and `kew deny` release or refuse a
  `needs-approval` hold, and they record who decided. Only the operator runs
  them.
- **`ready` labels.** Adding the `ready` label is what admits an issue to the
  queue. A human decides what agents work on; do not label issues yourself.
- **Merges.** Pull requests merge only after a human review. Do not merge,
  approve a pull request, or add any approval or merge label.

Everything else — doctor, init, setup, schema, status, skills — an agent may
run on its own.
