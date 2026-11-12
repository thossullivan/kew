# kew gc

List what closed work left on disk, and how much space removing it frees; `--apply` removes it.

```text
Usage: kew gc [OPTIONS]

Options:
  --apply   Remove exactly what the dry run lists.
  --images  Also list, and with --apply remove, stale kew-agent image tags.
```

kew keeps checkouts on purpose: a replaced agent checkout is preserved, a superseded review checkout is moved aside, and a sandbox clone outlives its run.
Some are removed elsewhere: the epic driver removes a PR's review checkouts once it sees the PR merged or closed, and `kew cleanup` and merge verification remove work-root entries past their seven-day retention that no run holds.
Nothing else removes them: a replaced or preserved agent checkout, and anything those miss, stays.
`kew gc` finds them, and by default it only lists them.

It lists, for an issue that is closed or a PR that is merged or closed:

- the agent checkout `<worktree_base>/agent/issue-<N>`, its `.preserved-*` and `.replaced-*` generations, and the `.lock` and `.generation` files beside each;
- the sandbox checkouts `kew-sandbox-<N>-*` in this ledger's directory of the work root, with their lock and generation files;
- the review checkout `<worktree_base>/review/pr-<N>` and its `.preserved-*` copies.

Each path is printed with its size, followed by a total in bytes.
Sizes are the apparent sizes of the files under each path.
A symlink named like a checkout or one of its files is never followed, listed or removed, and a checkout with such a link among its files is kept whole.
A checkout directory (`agent`, `review`, or the ledger's directory of the work root) that is itself a symlink is not entered.

The dry run changes nothing on disk and nothing in the container runtime.
It opens the run ledger read-only, so it never records the ledger's identity: a ledger that no kew command has registered yet is refused, and nothing is listed.
`--apply` removes exactly what the dry run lists.

## Agent image tags

Without `--images`, `kew gc` lists no image tags.
With `--images`, it also lists the `kew-agent`, `kew-agent-codex` and `kew-agent-opencode` image tags that `kew doctor` reports as stale: tags that no `[sandbox]` image setting, packaged pin or container uses.
`kew gc --apply --images` removes them; `--apply` alone removes no tag.
Image tags are printed with the size the container runtime reports, which can share layers with other images, so they are not in the byte total.

`kew gc` reads only this project's configuration.
A tag that another kew install, or another project on the same machine, pins looks stale here, and `--apply --images` removes it.
Pass `--images` only when no other kew install or project uses this container runtime.

With `--images`, a runtime binary that is not installed, or an inventory the runtime cannot list, is reported as an agent image inventory that could not be read.

## What is kept

`kew gc` reads the state of each issue and PR once, with `gh`.
It keeps every path of:

- an open issue or PR;
- an item whose state cannot be read, which is reported: a Linear work item, or any other that is not a GitHub issue, and the `kew-sandbox-smoke-*` clone that `kew sandbox smoke` leaves, are always kept, so a work root holding one makes the command exit 2;
- a closed issue with an open PR: one from its branch (`gh pr list --head <prefix><N> --state open`), or one a run of the issue recorded;
- a closed issue a run of which recorded a PR of another repository than `[project] repo`, which `kew gc` cannot judge;
- an item that holds a reservation, or a run that holds its tree: a pending run, a run awaiting publication or continuation, or a held publication, whatever the row's status says;
- an item with a continuation that is neither complete nor abandoned;
- a PR with a live review reservation or a pending review run, which keeps all of its review checkouts. `kew review` reserves the PR's owner issue, not the PR, so a reservation counts when it is on the PR, on an item a review run of the PR recorded, or is a hand-run review's naming the PR;
- the review copy that a deferred publication names, and every copy of a PR whose deferred copy is unknown.

A PR list that cannot be read keeps the issue's paths and is reported as a state that could not be read.

`kew gc` does not enter the work root when the sweep that removes expired work trees would not:

- the ledger's directory is the shared `unscoped` directory, which every unregistered project uses and no ledger can vouch for;
- the ledger's directory is a symlink, or does not resolve directly inside the work root;
- the work root has no `.kew-work-root` marker (kew did not create it), or it is the filesystem root, the home directory, the project checkout, or one of their ancestors.

It then keeps every sandbox checkout and says why; the work root counts as refused below.

## How `--apply` removes

A checkout another run holds the claim lock on is left in place and reported.
Once `kew gc` holds a checkout's claim, it reads the ledger again; a run, reservation or continuation that started after the listing keeps the checkout, which is reported.
A review checkout is removed only while `kew gc` holds the PR's review claim.
A review claim lock that is a symlink, or not a regular file, is never opened or written through: that PR's review copies are kept, and each is reported as a path that could not be removed.
The review claim's lock is unlinked only while `kew gc` still holds it, and only when `kew gc` created it or the review directory is gone.

`--apply` needs the project's git checkout, found from the project directory (the one holding `kew.toml`, or `--project`), never from the current directory.
When git cannot name one, `--apply` removes nothing and exits `2`: without it, a registered worktree would be deleted and its registration orphaned.

`kew gc` never runs git inside a checkout it removes.
A checkout tree is removed with its generation file first and its lock last.
A checkout the project's git checkout registered with `git worktree add` is first removed with `git worktree remove`, run from that checkout, which deletes its tree; its generation file is revoked after that, and its lock removed last. `git worktree prune` follows.
A failure is never silent: when `git worktree list` fails or cannot start, every checkout is kept; when `git worktree remove` fails or cannot start, that tree is kept; when `git worktree prune` fails, the failure is reported; each counts as a path that could not be removed.
An image tag is removed with the runtime's `image rm`, which untags it and leaves an image that another tag still names.

## Exit codes

Exit `0` when the listing printed and, with `--apply`, everything it listed was removed, `1` when a listed path or tag could not be removed, and `2` when an issue or PR state, the run ledger or the agent image inventory could not be read, or the work root was refused, or `--apply` found no git checkout.
When both apply, `2` takes precedence over `1`.
The command removes what it could before it exits.
See [exit codes](exit-codes.md) for the scheme.

## Caches kew does not own

`kew gc` does not touch caches that other tools manage.
Two of them grow with every dispatch and can be pruned by hand:

```bash
# uv's package cache, shared by every worktree's `uv sync`
uv cache prune

# BuildKit's build cache from agent image rebuilds
docker builder prune --keep-storage 10GB
```

**Example:**

```bash
# See what closed work left behind, and how much space it takes
kew gc

# Remove it
kew gc --apply

# Also remove stale agent image tags (only when no other kew install uses this runtime)
kew gc --apply --images
```
