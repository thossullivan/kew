# kew rename-repo

Move the ledger's rows from an old repository slug to the configured `project.repo`, after GitHub renamed the repository.

```text
Usage: kew rename-repo [OPTIONS]

Options:
  --from TEXT         The repository's old OWNER/NAME slug. [required]
  --apply             Rewrite the ledger; without it, a dry run.
  --from-name-reused  Another repository has taken the old name: skip the check
                      that GitHub redirects it to project.repo, and vouch for it
                      yourself.
```

The new slug is `[project] repo` in `kew.toml`; edit it first.
`--from` must be `OWNER/NAME`, each part starting with a letter or digit.
The command refuses when `project.repo` is empty, still equals `--from`, or differs from it only in letter case, and when `gh api repos/<new>` does not answer with that exact `full_name`.
GitHub names ignore case, so a case-only change is not a rename.

GitHub answers a renamed repository's old name with the new one until another repository takes the old name.
So `--apply` also requires `gh api repos/<old>` to answer `full_name` equal to the new slug, which proves the two are one repository.
Once the old name belongs to another repository, that check cannot pass: pass `--from-name-reused` to skip it and vouch for the rename yourself.

By default it is a dry run: it opens the ledger read-only and prints, per table and column, how many rows hold the old slug, how many run reports it would move, what it never touches, and anything that would refuse `--apply` right now, the GitHub check included.

## What `--apply` rewrites

One `BEGIN IMMEDIATE` transaction rewrites every row whose value is exactly the old slug in:

- `runs.repo`, `run_launches.repo`, `run_admission_identity.delivery_repo`, `loops.repo`;
- `merge_queue.repo`, `merge_queue_suspects.repo`;
- `publication_job.delivery_repo`, `human_review_policy.delivery_repo`.

It also rewrites the prefix `https://<host>/<old>/pull/` to `https://<host>/<new>/pull/` in `runs.pr_url` and `orphan_prs.url`, where `<host>` is the project repository's host (read from the origin remote, `github.com` when none names one).
In the same transaction it appends one [`repo_renamed`](../audit.md#repository-renames) audit event.
A failure at any point rolls back every row and the event.

The CI-fix attempts a PR used under the old slug count against it under the new one.

## What it never touches

`audit_events` (each row hashes its `repo`, so rewriting one would break `kew audit verify` and the published anchors), `epic_ticks`, `artifact_receipt` and `outcome_sync_jobs`.
`kew report provenance` and the merge-verify reading count audit rows recorded under the old slug when they came before the `repo_renamed` event, and stop counting them for whatever repository takes the old name.

## When it refuses

`--apply` writes nothing and exits `2` while any of these holds, each read again inside the transaction:

- a drive holds its lock;
- a `kew serve` sweep beat within the last ten minutes;
- a run is in flight, or an epic reservation is held;
- an epic driver lock file is held;
- a loop of the old slug is pursuing and the console does not read it as stale (it has written something within its staleness window, or one of its stamps cannot be read);
- a merge claim has not expired, or a merge-queue entry is queued, merging or landed;
- a stop-label reset is pending (its delivery reads the old slug from the hashed request);
- a unique key would collide: `human_review_policy` or `merge_queue_suspects` holds a row for both slugs, or `run_launches` numbers a CI-fix slot the same for one PR under both.

A paused loop, a stale pursuing one, and a loop of another repository or with no repo do not refuse.
A paused loop's row is rewritten with the rest, so a later `kew loop resume` acts on the new slug.

It also refuses before it opens the ledger when the GitHub check above fails without `--from-name-reused`.
It refuses when nothing holds the old slug (no ledger row, no run report, no earlier rename), so a mistyped `--from` records nothing.

A rename is recorded once.
After it, rows that hold the old slug again were written under that name since: by a process still configured with it, or by the repository that took the name.
`--apply` then refuses rather than append a second event; find what wrote them and fix those rows by hand.

## Run reports

After the commit, each run report in `<sink_path>/<old-slug>/` moves to `<sink_path>/<new-slug>/`.
The move changes two fields and keeps every other byte: `repo` when it is the old slug, and `pr_url` when it starts with the old pull request prefix.
Each is changed in the frontmatter and in the body line that renders it (`- **repo**: ...`, `- **pr_url**: ...`), so both read the new repository.
Nothing is exported again, so the evidence ids, test-integrity fields, keys an indexer added, and every other body line, prose that names the old slug included, stay as they were.

Each move writes the target first and removes the source after, so a stopped move leaves both.
The next `--apply` removes a source whose target already holds its moved form, and finishes a move an older kew left as two links to one file.
A source whose target differs, a symlink, or a file with no frontmatter stays where it is, is named, and makes the command exit `1`.
With `--from-name-reused`, GitHub no longer proves that the old name is this repository, and the repository that took the name may have written reports to the same directory.
So only the reports of runs this ledger records under the new slug move; every other report stays and is named, as above.
Remove the cause and run `--apply` again: with the rename recorded and no old-slug rows, it only moves the reports that are left.

When no row or run report holds the old slug and the rename is recorded, `--apply` prints `Nothing to do` and exits `0`.

## Exit codes

Exit `0` when the dry run printed or `--apply` renamed everything, `1` when a run report could not be moved or the ledger rolled back, and `2` when the rename was refused or could not run.
See [exit codes](exit-codes.md) for the scheme.
The full procedure is in [Renaming the repository](../operations.md#renaming-the-repository).

**Example:**

```bash
# After editing kew.toml to repo = "acme/new-name"
kew rename-repo --from acme/old-name

# Rewrite the ledger and move the run reports
kew rename-repo --from acme/old-name --apply
```
