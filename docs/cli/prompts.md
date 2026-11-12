# kew prompts

See and change the operator profile each agent role's prompt runs with.

A **profile** is an addendum to a role's packaged prompt, never a replacement:
the packaged prompt stays the reviewed base, and replacing a base still takes a
merged PR. A role runs its base plus at most one active addendum, rendered in
one block headed `## Operator instructions — <role> profile v<N>`. The fixed
safety and publication sections render after the addendum and take precedence
over it. The `adversarial_review` addendum renders before the whole reviewer
rubric, which follows a host sentence giving the review rules precedence.

Addenda live only in the operator's `dispatch.db`. Every proposal and decision
needs an actor in `[approval].approvers` (the acting name is `KEW_ACTOR`, else
the OS user), and each one writes a chained audit event before the row it
attests. The list is empty by default, and while it is empty every proposal and
decision is refused with "Set [approval].approvers in kew.toml before deciding
prompt profiles." A refused command writes nothing: the actor and the input are
checked before the ledger is opened, so a refusal on a project with no ledger
creates neither `dispatch.db` nor the ledger registry. Only `propose` may create
the ledger; `approve`, `decline` and `retire` with no ledger are refused. A lost
ledger (its database gone while its registry or audit anchors survive) is never
read as a new project: `list` and `propose` exit 2 on it, as `show`, `approve`,
`decline` and `retire` do, with a `REGISTERED-LEDGER-MISSING` reason.

```text
Usage: kew prompts [OPTIONS] COMMAND [ARGS]...

Commands:
  list     List each agent role, its prompt source and its active profile version.
  show     Print the prompt a role's launch renders, or why the launch refuses.
  propose  Propose an addendum for a role; it changes nothing until approved.
  approve  Activate a proposed profile version, retiring the one it replaces.
  decline  Decline a proposed profile version; what the role runs is unchanged.
  retire   Retire a role's active profile, leaving it on its base prompt.
```

## `kew prompts list`

One line per agent role: its prompt source, its active profile version (or
`none`), its generation and any proposed versions. Roles a profile may not
apply to say so.

```text
Usage: kew prompts list [OPTIONS]

Options:
  --json  Emit machine-readable JSON.
```

## `kew prompts show`

Prints the shape of the prompt the role's launch renders, over a fixed sample
item: for `dispatch` the served `[prompt]` template and append, and the
approved profile, whose addendum sits where the launch puts it, before the
fixed safety and publication sections, which say they take precedence over it.
The header names the profile state (`profile=none`, `v1`, or
`refused: <reason>`); a profile the launch would refuse prints the base prompt.
Each proposed version's text follows, so you can read a proposal before you
approve it.

When the launch would refuse before it builds a prompt (for example a
`[prompt] append` or `template` under host publication), no prompt is printed:
the header and proposals are, then the refusal, and the command ends as a
refusal does (see Exit codes below). `--json` gives both `base_prompt` and
`effective_prompt`; for a refused launch it also carries `"outcome": "refused"`
and the `reason` (as does `variant.refusal`).

```text
Usage: kew prompts show [OPTIONS] ROLE

Options:
  --json  Emit machine-readable JSON.
```

## `kew prompts propose`

Records the file's text as the role's next proposed version. A proposal changes
nothing a role runs until an approver activates it. The text is at most 4000
characters once whitespace and kew's reserved fence markers are stripped.

```text
Usage: kew prompts propose [OPTIONS] ROLE

Options:
  --file PATH  File holding the addendum's text.  [required]
  --json       Emit machine-readable JSON.
```

## `kew prompts approve`

Activates a proposed version and retires the addendum it replaces.

`--generation` is the role's generation **you saw** in `kew prompts list` or
`kew prompts show`. If another decision changed the role since, the approval is
refused as stale and nothing changes: look again, then decide again.

The approval also checks the version's text against the digest it was proposed
under, and against the rule `propose` applies (no line that opens a code fence
or an HTML block). A version that fails either check is refused and nothing
changes: decline it and propose it again.

```text
Usage: kew prompts approve [OPTIONS] ROLE VERSION

Options:
  --generation INTEGER  The role generation you saw in `kew prompts list` or `show`.  [required]
  --json                Emit machine-readable JSON.
```

## `kew prompts decline`

Declines a proposed version. What the role runs is unchanged.

```text
Usage: kew prompts decline [OPTIONS] ROLE VERSION

Options:
  --json  Emit machine-readable JSON.
```

## `kew prompts retire`

Retires the role's active addendum, leaving it on its bare base prompt. Like
`approve`, it takes the generation you saw, so it never retires a replacement
you did not see.

```text
Usage: kew prompts retire [OPTIONS] ROLE

Options:
  --generation INTEGER  The role generation you saw in `kew prompts list` or `show`.  [required]
  --json                Emit machine-readable JSON.
```

**Example:**

```bash
kew prompts list
kew prompts propose dispatch --file dispatch-addendum.md
kew prompts show dispatch                      # read v1 and note the generation
kew prompts approve dispatch 1 --generation 0
kew prompts retire dispatch --generation 1
```

## Exit codes

See the shared [exit-code scheme](exit-codes.md).

| Code | Meaning |
| --- | --- |
| `0` | The listing or preview was printed, or the decision was recorded. |
| `2` | Nothing was decided: the actor is not an approver or `[approval].approvers` is empty, the generation is stale, the version is not proposed, the role takes no profile, the role has no active profile to retire, the addendum is empty or too long, the file could not be read, there is no ledger to decide against, the ledger is lost (its database gone while its registry or anchors survive; `list` and `propose` exit 2 on it too), or the ledger could not be written (a lock held past the retry budget, a read-only file, or another SQLite error). For `show`, the role's launch would refuse before it builds a prompt. |
