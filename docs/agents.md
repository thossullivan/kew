# Agents

How a script or a coding agent sets up and operates kew without a terminal.
It is the [Getting Started](getting-started.md) path with every prompt
replaced by a flag and every report read as JSON.

## 1. Install

Install the CLI with `uv tool install`, as in
[Getting Started](getting-started.md#install-and-first-dispatch). The
prerequisites are the same, including a logged-in `gh` (`gh auth login`); an
agent cannot complete that login for a human, so check it first with
`gh auth status`.

## 2. Write the configuration with `kew init --yes`

```bash
kew init --yes --runner claude-code --repo owner/name \
  --ci "uv run pytest" --format "uv run ruff format ." --runtime docker
```

`--yes` asks nothing. Every value not passed as a flag comes from detection;
pass all of them when the result must not depend on what this machine detects.
`[agent] model` and `[budget] per_issue_usd` are never detected: pass
`--model` and `--budget-per-issue` to set them. The flags are listed in
[`kew init`](cli/init.md).

Without a terminal and without `--yes`, init refuses with exit `2`, writes
nothing, and names the flags it would have asked for. It never waits on a
prompt.

Init refuses an existing `kew.toml` with exit `2` and leaves it untouched.
Pass `--force` to overwrite it: that is how an agent changes a value, rather
than editing the file.

After writing, init runs `kew setup` (labels and security hooks) and the
`kew doctor` checks. With `--yes` it runs no fix itself: it prints each
failing check with its `fix:` command and exits `1`.

| `kew init` exit | Meaning |
| --- | --- |
| `0` | `kew.toml` written; setup and every check passed. |
| `1` | `kew.toml` written; setup or a readiness check needs fixing. Go to step 3. |
| `2` | Nothing written: an existing `kew.toml` without `--force`, an unknown runner, a file that would not load, or no terminal and no `--yes`. |

## 3. Loop on `kew doctor --json`

```bash
kew doctor --json
```

[`kew doctor --json`](cli/doctor.md#json-output) prints one JSON document on
stdout and nothing else there:

```json
{
  "schema": 1,
  "ok": false,
  "checks": [
    {
      "id": "allowlist-network",
      "status": "fail",
      "summary": "'kew-allowlist' is not present",
      "hint": "kew installs no firewall rules on it, so a plain bridge gives the container full egress until you restrict it yourself",
      "fix": "kew sandbox network",
      "details": [],
      "warnings": []
    }
  ]
}
```

The loop:

1. Run `kew doctor --json`. Stop when `ok` is `true` (exit `0`).
2. For each check whose `status` is `warn` or `fail`, run its `fix` command.
   `fix` is shell-quoted and never spends money. Typical fixes are
   `kew sandbox build --runner <runner>` and `kew sandbox network`.
3. Hand a check to a human instead when its `fix` is `null` (no single
   command fixes it; read `hint`), needs a secret (a `runner-credential` fix
   such as `export ANTHROPIC_API_KEY=<key>` carries a placeholder), or is
   interactive (`gh auth login`).
4. Run `kew doctor --json` again. If the same check fails twice after its
   fix, stop and report it rather than looping.

`skip` means a check could not run; it is never a pass.

## 4. Dispatch

```bash
kew run --dry-run   # which ready issue kew would pick; runs nothing
kew run             # dispatch the highest-priority ready issue
kew dispatch 42     # dispatch one named issue
```

Nothing dispatches until an issue carries the `ready` label.

## Commands and their JSON output

Every command and its flags are listed by `kew --help` and
`kew <command> --help`, and in the [CLI reference](cli/index.md).

These commands take `--json` and print one JSON document on stdout:

| Command | Reports |
| --- | --- |
| `kew doctor --json` | Readiness checks, with a `fix` per failing check |
| `kew setup --json` | What setup created or found |
| `kew status --json` | Running and recent dispatches |
| `kew health --json` | Health checks over the run store |
| `kew canary --json` | Runner verification |
| `kew config effective --json` | The configuration every command resolves |
| `kew audit verify --json` | Audit-chain verification |
| `kew audit policy --json` | Audit policy findings |
| `kew audit tail --json` | Recent audit events |
| `kew epic status --json` | Epic progress |
| `kew epic watch --json` | Epic events that need an operator |
| `kew triage queue --json` | The triage queue |
| `kew skills list --json` | Installed agent skills |

`kew dispatch --json`, `kew run --json` and `kew review --json` print one
[result summary](cli/dispatch.md#json-result-summary) on stdout when the
command ends, and keep progress on stderr.

[`kew schema`](cli/schema.md) prints every command as JSON, with its options
and whether it takes `--json`, so an agent can read the list above from the
installed version:

```bash
kew schema | jq -r '.commands[] | select(.json) | .command'
```

Read the exit code, not stdout, to decide what happened.

## Exit codes

Every command follows one two-axis [exit-code scheme](cli/exit-codes.md):

| Code | Did it run? | What did it find? |
| --- | --- | --- |
| `0` | Yes | Nothing to act on. |
| `1` | Yes | Findings: something needs attention or a repair was performed. |
| `2` | No | An operational failure or a missing prerequisite stopped it. |

On `1`, read the findings and act on them. On `2`, nothing ran: fix the
prerequisite named on stderr before retrying, and do not retry in a loop.
