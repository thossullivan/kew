# kew init

Write a project's `kew.toml` from what kew detects and the flags you pass, then run `kew setup` and the `kew doctor` checks.

```text
Usage: kew init [OPTIONS]

Options:
  --runner TEXT              Runner to dispatch with (default: detected).
  --model TEXT               Model the runner should use.
  --ci TEXT                  CI command agents must pass (default: detected).
  --format TEXT              Format command (default: detected).
  --runtime TEXT             Container runtime (default: detected).
  --repo TEXT                GitHub repository owner/name (default: detected).
  --budget-per-issue FLOAT   Spend cap per issue, in USD.
  -y, --yes                  Write without asking anything.
  --force                    Overwrite an existing kew.toml.
```

**Example**

```bash
kew init --yes --runner codex
kew init --yes --runner claude-code --budget-per-issue 25
kew init            # on a terminal: asks only what it could not detect
```

## What it detects

Every value comes from detection or a flag; a flag always wins.

| Value | Detected from |
|-------|---------------|
| `[project] repo` | `gh repo view` in the project directory |
| `[agent] runner` | The first runner CLI on `PATH`, in the order `claude-code`, `codex`, `opencode`, `pi`, `hermes`, `lemonade` |
| `[commands] ci` | A `ci` or `test` recipe in `justfile`, target in `Makefile`, or script in `package.json`; else `pytest` when `pyproject.toml` configures it |
| `[commands] format` | A `fmt` or `format` recipe, target or script; else `ruff format .` when `pyproject.toml` configures ruff |
| `[sandbox] runtime` | `docker`, then `podman`, on `PATH`. `openshell` is chosen with `--runtime` only |

`[agent] model` and `[budget] per_issue_usd` are never guessed: they are written only when you pass `--model` or `--budget-per-issue`.
A `pyproject.toml` command is prefixed with `uv run` when the project has a `uv.lock`.

## What it writes

The smallest valid file: a value equal to kew's default is left out, so the file holds only what this project changes.
The file is validated by the same loader every kew command uses before anything is written.
With `--yes`, init asks nothing.

`kew init` never overwrites an existing `kew.toml`: it refuses and leaves the file byte-for-byte untouched unless you pass `--force`.

## On a terminal

When stdin is a terminal and `--yes` is absent, init asks for what neither a flag nor detection answered: the repository, the CI and format commands, and the container runtime, each showing kew's default.
It always asks for the runner unless you pass `--runner`, with the detected runner as the default, after one screen comparing every runner's containment posture and what it costs:

```text
runner       installed  posture    what it costs
claude-code  no         container  an image to build, and ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN exported
codex        yes        native     nothing to build; it keeps its own CLI login
lemonade     no         host only  no boundary: it runs on this host with your access
```

`[agent] model` and `[budget] per_issue_usd` are never asked; pass them as flags.
Init then prints the file and asks before writing it.

After writing, init offers to run the fixes it can run itself: `kew sandbox build --runner <runner>` when the runner image check fails, and `kew sandbox network` when the `allowlist-network` check fails.
When a `claude-code` credential is missing it says how to get one: run `claude setup-token`, then `export CLAUDE_CODE_OAUTH_TOKEN=<token>`.
It then re-runs the checks; a fix you declined is still listed, with its `fix:` command.

Every answer maps onto a flag, and init ends by printing the `kew init --yes ...` command that writes the same `kew.toml`, so an agent or script can repeat the run.
That command passes every chosen value, so it does not depend on what the next machine detects.

Without a terminal and without `--yes`, init asks nothing: it refuses as a failure to run, writing nothing, and names the flags it would have asked for instead of waiting on a prompt nobody can answer.

## After writing

Init runs [`kew setup`](setup.md), then the [`kew doctor`](doctor.md) checks, and prints each result with its `fix:` command.
Among them is `runner-credential`: a contained runner cannot inherit a CLI login, so its credential has to be in the environment.
When it is missing, init still writes the file and prints the fix, for example `fix: export ANTHROPIC_API_KEY=<key>` for `claude-code`.
A runner that contains itself natively (`codex`), an OpenShell runtime, and a project with containment disabled skip this check; with `dangerously_skip_containment` it warns that dispatch runs the runner uncontained.

When nothing needs fixing, init prints the first command to run, `kew run --dry-run`.

## Exit codes

See the `init` row of the shared [exit-code scheme](exit-codes.md).
A file that was written but still has something to fix is a finding, not a failure to run.
