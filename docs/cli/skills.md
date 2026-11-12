# kew skills

Install and maintain kew's shipped agent skills and the security-guard hook inside a target repo's `.claude/` directory. Artifacts install **from the wheel's package data only** — no remote fetch, no registry. See [Skills catalog](../skills.md) for what's in the box.

```text
Usage: kew skills COMMAND [ARGS]...
```

```text
Usage: kew skills list [OPTIONS]

Options:
  --json  Print one JSON document instead of a table.
```

```text
Usage: kew skills install [OPTIONS] [NAMES]...

Options:
  --all
  --force
```

```text
Usage: kew skills update [OPTIONS] [NAMES]...

Options:
  --force
```

```text
Usage: kew skills diff [NAMES]...
```

```bash
kew skills list                          # state of every shipped artifact
kew skills install --all                 # install everything not yet installed
kew skills install plan-to-issues        # install one artifact by name
kew skills update                        # converge ALL installed + missing artifacts
kew skills update security-guard         # refresh one artifact
kew skills diff                          # show drift for every artifact
kew skills diff handoff                  # show drift for one artifact
```

## State machine

`kew skills list` classifies each artifact's installed copy:

| State | Meaning |
|---|---|
| `not-installed` | Nothing at the target path yet. |
| `current` | Installed copy matches the shipped content exactly. |
| `outdated` | Installed copy matches a prior shipped version — safe to auto-update. For the `security-guard` hook, this means the file still carries the `# managed-by: kew` header but its content no longer matches the current re-render (including a hand-edit that left the header in place); `update` overwrites it silently. |
| `locally-modified` | Installed copy doesn't match any known shipped version — someone hand-edited it. For the `security-guard` hook specifically, this only fires once the `# managed-by: kew` header has been removed or the file replaced outright — kew still owns (and will overwrite) any file carrying that header. |

`kew skills list --json` prints the same rows as one JSON document on stdout:

```json
{
  "schema": 1,
  "skills": [
    {"name": "handoff", "kind": "skill", "version": 1,
     "target": ".claude/skills/handoff/SKILL.md", "state": "current",
     "installed": true, "drift": false}
  ]
}
```

`state` is one of the four states above. `installed` is `false` only for
`not-installed`. `drift` is `true` only for `locally-modified` — the hand edit
`kew skills diff` shows and `update` refuses to overwrite without `--force`.
An `outdated` copy is kew's own older version, so it is not drift.

Skills compare by sha256 against the manifest's current and historical hashes. The `security-guard` hook compares differently: it re-renders the template with your current `kew.toml` and checks for the `# managed-by: kew` header rather than matching against a fixed set of historical hashes — see [Guard updates preserve your config](../skills.md#guard-updates-preserve-your-config) for why that matters and where to put durable customizations.

## `kew skills install [names…] [--all] [--force]`

Installs the named artifacts, or every artifact with `--all`. Refuses to overwrite a `locally-modified` copy unless `--force` is passed. Exits `1` for locally modified artifacts.
Exits `2` for unknown names or an empty selection without `--all`.

## `kew skills update [names…] [--force]`

**No names = converge everything**, not just what's already installed: it refreshes every `outdated` artifact to the current shipped version *and* installs every `not-installed` one. Pass explicit names to scope the update to specific artifacts (still refusing local modifications without `--force`). This is the command `kew setup --with-skills` and `kew setup`'s printed tip both point at.

`--force` overwrites `locally-modified` copies with the shipped version, discarding the local edits. `kew skills diff` first to see what you'd lose.

## `kew skills diff [names…]`

Prints a unified diff of installed-vs-shipped for each artifact that has drifted (empty output for artifacts that are `current` or `not-installed`). Defaults to every artifact when no names are given.

## Runner scope (v1)

Skills at `.claude/skills/<name>/SKILL.md` are read by **Claude Code only** — including headless (`claude -p`) dispatch runs. The `opencode`, `pi`, and `lemonade` runners do not read `.claude/skills/` in v1; installed skills have no effect on dispatches routed through them. See [Runners](../runners/index.md) for routing.

Official Codex does not read these `.claude/skills/` artifacts. It may read
regular repository policy under `.codex/rules/` and `.codex/skills/`; kew
rejects `.codex/config.toml`, unknown entries, symlinks, and special files.

## Exit codes

See the shared [exit-code scheme](exit-codes.md).

| Code | Meaning |
|---|---|
| `0` | All selected artifacts applied (or already current) / no diff found. |
| `1` | A diff was found, or a locally modified artifact was refused without `--force`. |
| `2` | Unknown artifact, missing selection, or configuration failure. |
