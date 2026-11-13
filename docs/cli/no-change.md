# kew no-change

Decide one no-change outcome: confirm it, reject it, rejudge it against the
current base, or retry a synchronization that held.

A no-change outcome is a run that finished with **no diff to show**. kew does
not close the work item on its own — it records an attested judgment and waits
for a person, because an item closed on an absence leaves a reviewer nothing to
read afterwards. This command group is that decision from the terminal; the web
console's **Judgment queue** offers the same three actions against the same
service verb, with the same authority rules and the same audit event.

Each command names the **work item** — the GitHub issue number or tracker id,
the same spelling `kew dispatch` and `kew triage` take. kew resolves the
judgment that currently owns that item's outcome, so a decision can never be
pointed at a different one, and an item with nothing held on an absence is
refused rather than silently doing nothing. The resolved judgment id is printed
with the result, for the audit trail.

```text
Usage: kew no-change [OPTIONS] COMMAND [ARGS]...

Commands:
  confirm     Authorize one no-change outcome and synchronize it to its source.
  reject      Refuse one no-change outcome. Closes nothing, unblocks nothing.
  rejudge     Re-check one no-change against the current base, and record it.
  retry-sync  Re-run a synchronization that held, for an outcome already decided.
```

## `kew no-change confirm`

Revalidates the base the judgment was made against, then closes the source item
with a disposition note. A no-change is a claim about a commit, so a base that
has moved refuses the confirmation — unless the current base descends from the
judged one and no commit since touches a **judged path**. The refusal names the
paths that were touched and the command that moves the item forward,
`kew no-change rejudge <item>`. An outcome that was already decided cannot be
rejudged, so a stale synchronization names `kew no-change reject <item>`
instead.

The judged paths are the tracked files the work item's title and body name, read
at the judged base (`src/app.py:12-30` counts as `src/app.py`). A bare file
name such as `waits.py` or `README.md` counts when exactly one tracked file in
the repository has that name, the root included; a name with a directory, such
as `graph/driver/tick.py`, counts when exactly one tracked file ends in it. A
word that is the name of a tracked file, such as `Makefile` or `Dockerfile`,
is a path name too. The narrowing is all or nothing: paths are recorded only when
**every** path-shaped name in the item resolves to exactly one tracked file. If
any name does not resolve (an untracked or ignored file, a typo, a dotted code
name), is ambiguous, names a directory or a glob, or the item names no path at
all, the judgment records no judged paths and stays bound to the exact base it
was made on: any move of main makes it stale. The tracked files, the descent
and the touched paths are read with git at the top level of the project's
repository, even when `kew.toml` sits in a subdirectory; a base the repository
does not hold refuses as stale.

```text
Usage: kew no-change confirm [OPTIONS] ITEM

Arguments:
  ITEM  Work item: a GitHub issue number (2445) or a tracker id (SFD-12).
```

## `kew no-change reject`

Settles the judgment as refused and posts the reason to the item. Closes
nothing, unblocks no dependent, and re-dispatches no work: what to do instead is
the operator's next decision. The base is recorded but never enforced, so an
item whose base has moved can always be refused.

```text
Usage: kew no-change reject [OPTIONS] ITEM

Arguments:
  ITEM  Work item: a GitHub issue number (2445) or a tracker id (SFD-12).

Options:
  --reason TEXT  Why the absence was not the answer. Required.
```

## `kew no-change rejudge`

Re-reads the current base, re-checks the judgment against it, records it as
the base the judgment stands on, and leaves the outcome pending, so
`kew no-change confirm` is still the next decision. No agent is dispatched and
no run is recorded. It takes the same authority a confirmation does, and only
pending outcomes can be rejudged.

The re-check is the confirmation's own: rejudge **refuses, and records
nothing,** when the commits since the judged base touched a judged path (for a
judgment that records none, any file), when the current base does not descend
from the judged one, or when the comparison could not run. The refusal names
the touched paths or the reason. After looking at those changes, pass
`--accept-changes --reason "…"` to record the new base anyway. The output
always lists the paths touched since the judged base. The
`no_change_rejudged` audit event is written once the new base is recorded, and
holds the old and new base, the judged and touched paths, the actor, and
whether changes were accepted and why.

```text
Usage: kew no-change rejudge [OPTIONS] ITEM

Arguments:
  ITEM  Work item: a GitHub issue number (2445) or a tracker id (SFD-12).

Options:
  --accept-changes  Record the new base even though the commits since touched
                    what the outcome is about, or could not be compared. Needs
                    --reason.
  --reason TEXT     Why the changes leave the no-change standing.
```

## `kew no-change retry-sync`

Re-runs the synchronization for an outcome a person already decided. Safe to
press repeatedly — the reconciler looks for this judgment's own marker on the
item before writing anything.

An outcome **nobody has decided yet** is refused rather than retried: the
synchronization is created by the confirmation, so before it there is nothing
to re-run and nothing to report. Confirm or reject the outcome first.

```text
Usage: kew no-change retry-sync [OPTIONS] ITEM

Arguments:
  ITEM  Work item: a GitHub issue number (2445) or a tracker id (SFD-12).
```

**Example:**

```bash
# See that the item's outcome is a zero-diff judgment waiting on a person
kew status --run run-2445-1

# Close the item on the recorded absence
kew no-change confirm 2445

# Main moved past a file the item names: rejudge refuses and lists the files;
# look at those changes, then record the new base and confirm
kew no-change rejudge 2445
kew no-change rejudge 2445 --accept-changes --reason "the rewrite keeps the fix"
kew no-change confirm 2445

# Or refuse it — the item stays open and unsatisfied
kew no-change reject 2445 --reason "the work was never attempted"

# The tracker was down when the outcome was confirmed; try the write again
kew no-change retry-sync 2445
```

## Who may decide

Exactly one of two rules is ever in force.

- A **non-empty** `[human_review].approvers` (falling back to
  `[approval].approvers`) requires membership by name. A repository admin who
  is not listed is refused: the point of naming people is that the repository's
  permissions stop deciding.
- An **empty** approvers list requires fresh `write`, `maintain` or `admin`
  permission on `[project].repo`, read at the moment of the decision.

Under the permission rule, the identity checked is the **forge login `gh` is
authenticated as** — the same account kew publishes with — not the local OS
username. The two differ on plenty of machines, and asking the forge about a
name that is not an account answers `none`. Both names are recorded in the
audit event: `forge_actor` is the login the permission was read for, and
`operator_actor` is the local identity (`KEW_ACTOR`, else the OS user) that ran
the command.

Authorship of the item is not a third rule and waives neither of the two. A
login whose permission cannot be read is refused exactly as a `read` one is: an
unread permission is not write access.

## Exit codes

See the shared [exit-code scheme](exit-codes.md).

| Code | Meaning |
| --- | --- |
| `0` | The decision was recorded. |
| `2` | Nothing was decided: the actor may not decide this outcome, the item names no live outcome, the outcome's state does not admit the command (a `confirm` of an outcome already authorized, a `retry-sync` before any decision, a `rejudge` of an outcome already decided), the item id is not canonical, the base moved under a `confirm`, a `rejudge` found the commits since touched a judged path, found a base that does not descend from the judged one, or could not compare them (without `--accept-changes`), `--accept-changes` came without a `--reason`, the current base could not be read, or no ledger was found. The one exception: a `rejudge` that recorded the new base but could not write its audit row exits with this code too, and says so, naming the old and new base. |

A refusal always names the rule that was in force and the login it was decided
about, so the fix is readable from the message.
