# kew review

Gather PR review comments and dispatch the configured runner to address them.

Standalone review supports runners that require **host-publication**, including
official Codex. Canonical mode always uses host publication. The review prompt
tells the runner not to commit or push. Kew commits the remediation in a fresh
canonical repository. Kew then pushes the existing PR branch from that repository.

Host publication does not establish that the runner lacks GitHub mutation
authority. The `review.md.j2` template can request comment replies when
`review.auto_reply` is enabled. This setting is independent of host publication.

Checkout mode has one agent-publication path. An agent-style runner on an
attached, uncontained checkout commits and pushes its own changes. Kew commits
and pushes for host-publication runners, detached checkouts, and contained runs.
A failed host publication preserves the checkout and returns non-zero. The
checkout is a standalone clone. The governed adversarial-review path remains
externally contained and uses OpenCode.

**Branch ownership.** A standalone `kew review <PR>` is given no work item, so
it resolves the PR's owning item from the head branch before it records its run:
the item the ledger's runs for that branch name. The run then names the branch
the way a governed remediation does, and its publication records the intent that
binds the pushed head to it under that item. Without that, the next review round
on the same PR refused to launch until an operator ran `kew publication attest`
by hand. A branch with no owning item at all keeps branch-scoped publication
intent. A branch with two owning items keeps the legacy newest-run heuristic.

Owning the branch does not put the review on that item's worker slot: the run is
still counted against `parallel.max_workers` under its own key, so a hand-started
review and a driver's run on the same item never share one slot.

Naming the branch does make a contained review's output visible on it, which is
the point: a head that no publication claim and no operator attestation names is
no longer bound by the newest-run heuristic on such a branch, because that head
may carry the contained run's output. Kew already answers that way for every
other contained run on a branch; before the review row named its branch, the
same output was published anyway and the heuristic bound the head silently. Bind
an unrecognised head on such a branch with `kew publication attest`. The heads
kew pushes itself are claimed as they are pushed and need no attestation —
removing that step for the review's own head is what the ownership fix buys.

```text
Usage: kew review [OPTIONS] PR_NUMBER

Arguments:
  PR_NUMBER       PR number to review (required)

Options:
  --author TEXT    Filter comments by author
  --dry-run       Show comments without dispatching
  --force-budget  Skip the standalone review budget pre-check
  --governed      Run one governed adversarial review round on a kew-published PR
  --force         With --governed: review an item a running epic drives
  --json          Print one JSON result summary on stdout; progress stays on stderr
```

**What it does:**

1. Fetches PR metadata and links the originating issue
2. Collects review comments, inline suggestions, and issue comments
3. Filters out resolved threads (via GraphQL) and bot comments
4. Builds a structured review prompt from the `review.md.j2` template
5. Materializes a dedicated review checkout at `<worktree_base>/review/pr-<n>`
6. Dispatches the configured runner to address each comment
7. Publishes the existing PR branch according to the selected publication mode
8. Records a durable `review.outcome` audit event and notifies it

**Verification scope.** The prompt depends on where the round runs, not only on
whether it is governed:

- A standalone `kew review` on the host runs the full test suite and the
  `[commands] ci` command, then the repo-wide policy gates on their own.
- A standalone `kew review` that kew runs in its container gets the targeted
  brief, the same as a contained governed round. It never runs the full suite,
  which does not fit in the sandbox's memory cap.
- A governed remediation round (an epic node's `NEEDS_CHANGES` verdict) runs the
  tests that cover the changed files plus a regression test for each finding.

A targeted brief lists the test modules kew selected. When the round's findings
or comments name files in the checkout, the list covers those files, the files
that import them (read through `kew verify`'s import graph) and the files a base
merge left conflicted. It does not cover every module the PR changes. A round
whose findings name no file keeps the whole-PR list.

The brief also has the agent:

- lint and format the touched files;
- run the type checker, the complexity gate and bandit on them;
- run the browser checks for the surfaces it touched on the merge candidate
  once the fix is committed, in a throwaway `git worktree add --detach` that is
  removed afterwards, never by merging in the agent's own tree, and not while a
  merge is in progress there;
- skip the full suite, `verify.sh`, semgrep and the docs checks.

A contained brief names the memory cap kew enforces on that runtime:
`[sandbox] max_memory`, or 8 GB. On OpenShell it says that kew enforces none.
The full verification runs before any merge.

**Before a governed review round.** When `[commands] static` or
`[commands] ui_check` is set, the epic drive runs those checks on the head
before it launches a reviewer (see [`commands.static`](../configuration.md)). A
failure becomes a remediation round with the output. A check that could not
launch, timed out, or names a tool the image lacks is skipped, not counted
against the head, and so is one that fails on the base too. A check whose
output overflows the 256 KiB budget is a failure, not a skip: the kept output
is its evidence, and the base run decides as for any other failure.

**Review checkouts never collide with implementation work.** The review checkout
is its own directory, never the `agent/issue-*` clone an implementation
agent may still be writing to. Before materializing, kew looks for an existing
checkout that already has the exact PR branch checked out. When it finds one, the review
runs **detached** at the PR head instead of creating a second local branch
(which used to abort the review with a git exit 255). A dirty implementation
checkout is never reset, cleaned, overwritten, or shared, and two concurrent
reviews of the same PR do not share a tree — the second one refuses.

**A PR the drive already holds is refused before checkout.** When a live
review holder (an epic's `kew epic _review-node` child, or another review run)
holds the PR's item, or a container a run on this PR would take already exists
(`kew-<pr>`, or `kew-<item>` for the drive's own runs), `kew review <PR>` names
that holder and exits 2 without creating a review checkout or starting a
container. Run it again once the named run finishes; a stopped container is
named with the `rm` command that removes it.

A holder whose recorded owner process is gone does not block. One whose owner
kew cannot probe (another machine, or no recorded owner) blocks until its run
has been pending past the in-flight bound. A run a dead `_review-node` child
started holds the PR while its container or sandbox still runs. A reservation
whose run never started within the drive's startup grace does not block.

**The review then takes the item's review reservation**, the same one the drive
takes before it reviews or remediates, and gives it back when it ends: on
success, on failure, or on an interrupt. While it holds it, a drive tick due to
review or remediate that item launches nothing, records one
`review_deferred_to_hand_run` event naming this review's run, and shows the node
waiting on it. If the drive took the reservation first, `kew review <PR>` exits
non-zero naming the holder's run id, pid and start time, whatever the two
containers are named, and even when the container runtime cannot be asked.
While this review's own process runs, nothing in the drive gives its reservation
back: not the tick, not the per-round sweep of `kew drive`, not the kill of a
merged or closed PR's reviewers, even after its agent's run has ended and it is
still publishing. That holds whether or not `kew review` leads its own process
group (under `uv run`, a script or a pipeline it does not). A
review killed outright (`kill -9`) does not keep the item: once its recorded
process is gone, and the agent container it started if it got that far, the
drive's next tick gives the reservation back and reviews the item itself.

An OpenShell sandbox that never got its agent started (the run's leader died
between creating the sandbox and starting the agent in it) reads alive only for
the drive's startup grace; after that it reads dead and the reservation it held
is released.

Publication is bound to the PR head the review was cut from: if the head moved
while the runner worked, kew refuses to push rather than publishing over a state
nobody reviewed, and preserves the checkout.

**A contained review is published by the host before the clone is deleted.** A
container holds no GitHub credential. The prompt tells the agent not to commit
or push. In canonical mode, host Git uses a fresh canonical repository. It reads
the review checkout only as plain file data. In checkout mode, the host commits
and pushes from the review checkout. A failed push preserves the clone, names
its path, and returns non-zero. A review-sensitive path also preserves the
checkout for human action.

**A PR branch that exists only on origin is fetched first.** Reviewing somebody
else's PR is the ordinary case where the operator's repository has never had
the head branch, so the review clone carries no ref for it. Kew fetches
`origin/<head>` into the review clone (never into the operator's repository)
before placing the checkout on the PR head.

**Outcomes survive the process.** Completion and failure — including a failure
*before* the runner ever launches — are persisted to the audit trail and pushed
to the configured notifier, so a `kew review` run needs no attached watcher:

```bash
kew audit tail --event review.outcome
```

**Examples:**

```bash
# Preview review comments without dispatching
kew review 42 --dry-run

# Dispatch agent to address all review comments
kew review 42

# Only address comments from a specific reviewer
kew review 42 --author octocat
```

## Governed review of a standalone PR

`kew review --governed <PR>` reviews a PR rather than remediating its comments.
It runs ONE round of the governed adversarial review an epic runs on each node,
for a PR dispatched on its own with `kew dispatch <n>` that no epic drives. It
uses the epic review stage's own per-node action: the same sandboxed reviewer
launcher, containment, verdict sidecar parsing, round recording, round cap,
residual stop rule, attested verdict, `kew-review` commit status and verdict
comment on the PR thread. The reviewer's run is recorded as an
`adversarial_review` run, so `kew report` attributes it as a review run.

The item is the one the PR's head branch names (`<branches.prefix><item>`).
Rounds are keyed on that item and recorded under the `standalone-review` epic
ref, so an epic that later adopts the item reads the same history.

```bash
# Review PR 2555: one reviewer round, verdict posted to the PR
kew review --governed 2555

# Same head, findings standing: remediates them (the governed remediation leg)
kew review --governed 2555

# After the remediation commit lands: reviews round 2 on the new head
kew review --governed 2555
```

What one run does depends on the item's recorded review state, exactly as an
epic tick's review pass does:

- No verdict on the current head: a reviewer round runs and its verdict is
  recorded and posted.
- `NEEDS_CHANGES` standing on the current head: the findings are remediated.
  At `[review] max_rounds` remediation rounds the item is held
  `review_rounds_exhausted` (blocking) instead; a round the stop rule accepts
  files its residuals.
- `READY` (or accepted residuals) already on the current head: nothing runs.

Before any reviewer runs it refuses, with one sentence, a config
with `[review] adversarial` off, a PR that is not open, a head branch that is
not a kew branch, a head that no kew publication evidence names (no host
`publication_intent`, normalized publication job or `kew publication attest`
claim for that exact head), an item held under any epic ref (the refusal names the
`kew epic unhold <ref> <item>` that releases it; this verb's own holds are
recorded under `standalone-review`), an item a reviewer or
remediator is already running for, an item whose issue carries an `epic:<name>`
label for an epic kew drives and that has not signalled complete (the refusal
names the epic; its review stage reviews the item, and `--force` reviews it here
anyway; labels GitHub cannot return also refuse), and a reviewer round the item's
review pool (`[review] max_review_usd`) has already spent. A head that no longer merges into its base
is not integrated: resolve it with `kew resolve <PR>` and run again.
`--governed` takes none of the comment-review options.

## JSON result summary

`kew review <PR> --json` prints one JSON document on stdout once the review
ends — the run id, outcome, PR URL, cost and duration, plus `reason` for a
refusal or failure — while progress, and the `--dry-run` comment listing, go to
stderr. The shape is the one
[`kew dispatch --json`](dispatch.md#json-result-summary) prints, and the exit
code is unchanged. A refusal before a run is minted carries `"run_id": null`.

## Exit codes

See the shared [exit-code scheme](exit-codes.md).

| Code | Meaning |
| --- | --- |
| `0` | Review completed; no review work; dry-run preview; `--governed` READY, accepted residuals, dispatched remediation or a standing verdict. |
| `1` | Runner or publication failure; `--governed` NEEDS_CHANGES (the message names the finding count), a hold or no verdict this run. |
| `2` | PR metadata or head branch unavailable; `--governed` refusal before any reviewer runs; budget refusal; connectivity failure; checkout failure; pre-launch refusal (not_run); configuration failure. |

Messages and run records retain raw runner codes.
The command boundary maps every nonzero executed result to `1`.
