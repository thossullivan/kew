# kew merge

`kew merge check` applies the merge verdict rule to a hand merge
([below](#checking-a-hand-merge)). `kew merge queue` is the project's merge
queue: one line of READY PR heads, described in most of this page.
`kew merge claim` holds a PR head while you verify it some other way, so the
drive pushes nothing onto it meanwhile
([Holding a PR head by hand](#holding-a-pr-head-by-hand)).

## The merge queue

The project's merge queue: one line of READY PR heads, kept in the dispatch
ledger and shared by every session on the host. Each PR is verified with
[`kew verify`](verify.md) on top of the current main before it merges, so a
merge needs no green GitHub checks.

```text
Usage: kew merge queue [OPTIONS] COMMAND [ARGS]...

Commands:
  add     Queue a READY PR head behind every entry already queued.
  list    Show the queue in the order it merges.
  remove  Take a PR out of the queue.
  run     Verify and merge the head of the queue once, as the drive does.
```

## Checking a hand merge

```text
Usage: kew merge check [OPTIONS] PR HEAD

Arguments:
  PR    The pull request number.  [required]
  HEAD  The head commit to merge, all 40 hex digits.  [required]

Options:
  --override-reason TEXT  Merge over a NEEDS_CHANGES or NEEDS_FULL verdict;
                          attested with your login, or KEW_ACTOR when set.
```

`check` applies kew's one merge verdict rule to a head you are about to merge
by hand. Every merge the epic driver makes, `kew epic merge`, `kew drive` and
the merge queue apply the same rule. It reads the newest
`adversarial_review_verdict` audit event recorded for exactly `HEAD`. A verdict
recorded for any other head reviewed other code and is ignored. UNPARSEABLE and
DEGRADED rows are skipped, and so is any row whose `error_category` names a
failed reviewer round (a launch that never ran, a provider usage limit, a
stream with no verdict): they record a kew fault, not a verdict on the code, so
a crashed round after a NEEDS_CHANGES does not hide it. Any other
`error_category` leaves the verdict standing.

The rule refuses while that newest verdict is NEEDS_CHANGES, or NEEDS_FULL: a
review round that still owes an independent full round at this head.

`check` does no forge read. It judges the sha you pass, not the PR's current
head, so pass the same sha to the merge itself:
`gh pr merge PR --match-head-commit HEAD`. GitHub then refuses the merge if the
PR's head moved after the check.

Exit codes follow the [exit-code scheme](exit-codes.md). The check returns `1`
and names the verdict while that newest verdict is NEEDS_CHANGES or NEEDS_FULL,
or when it is not one of kew's verdicts. It returns `2` when `HEAD` is not a
full 40-character sha, the ledger is missing or cannot be read, a review verdict
row in it does not decode (the message names its audit event), or the override
reason is blank. A row that does not decode refuses every head, because it
could be the newest verdict on any of them.

With `--override-reason`, a NEEDS_CHANGES or NEEDS_FULL check passes and writes
one `merge_verdict_override` audit event naming you (`KEW_ACTOR` when it is
set, else your login), the head, the PR and the reason. An override never
passes a verdict kew does not know or a ledger it cannot read.

A head with no verdict passes the rule. The drive's merges in an epic whose
review is blocking still need a READY review round for the head; in an epic
whose review is advisory, and for a node grandfathered from before review
applied, the rule is the only verdict check the driver's merge pass makes. The
driver has no override: a node whose head stands refused waits for a new review, or for
an operator's hand merge after an attested override.

## Queueing

```text
Usage: kew merge queue add [OPTIONS] PR

Arguments:
  PR  The pull request number.

Options:
  --head TEXT     The READY head commit, all 40 hex digits.  [required]
  --session TEXT  Who is queueing; defaults to KEW_SESSION or user:pid.
```

`add` refuses a PR that is not open at exactly `--head` on GitHub, a draft, a
PR that does not merge into the configured repository and base branch
(`[branches] base`, `main` by default), and a head kew's own review did not
find READY: the latest review round recorded for that head must be READY, or
a NEEDS_CHANGES round whose residuals the stop rule accepted. That review binds
the head to one node, whose acceptance contract the verify runs, so a head
found READY for two nodes is refused too. A PR that is
already queued at that head keeps its place; queued at another head, it is
refused until you `remove` it.

```text
Usage: kew merge queue list [OPTIONS]

Options:
  --all  Include merged, failed and dropped entries.
```

`list` marks an entry a pass has claimed to merge, with the pass that claimed
it. `list --all` also shows merged, failed, dropped and removed entries with
the reason for each.

```text
Usage: kew merge queue remove [OPTIONS] PR

Arguments:
  PR  The pull request number.
```

`remove` refuses an entry a pass has claimed: its merge may already be on its
way, and the queue never records a PR as removed when main holds it.

```text
Usage: kew merge queue run [OPTIONS]

  Verify and merge the head of the queue once, as the drive does.
```

## How a pass runs

[`kew drive`](drive.md#merging) takes one pass every round; `kew merge queue
run` takes one now. A pass holds the same repo-wide lock as `kew epic merge`
and the drive's standing merge rule, and a second lock kept beside the
dispatch ledger, so only one verification and one merge run at a time,
whichever session started them and whichever `kew.toml` it read. It takes the
first `[merge_queue] train` entries (one by default) and:

1. reads each PR on GitHub. A PR merged outside the queue at its queued head
   is recorded merged; one merged at any other head is dropped, since the
   queue merged nothing. A closed PR, a PR that no longer targets the
   configured base branch, or a head that changed while queued, is dropped
   with the reason;
2. fetches main once, and builds the candidate the way GitHub will land it:
   each queued head squashed, in queue order, onto main and the squashes
   before it. Before verification, it checks each head's net paths against
   `[epic] merge_gate_paths`. A gate edit without a `kew epic unhold` receipt
   for that exact head holds that PR and raises attention. The same hold
   applies when a READY review did not answer a removed assertion, changed
   expected value, added skip, or another test-integrity flag, and when the
   review names checks its sandbox could not run (the rules are
   [`kew epic merge`'s](epic.md#the-serial-merge-queue-opt-in)). One hold
   names every reason a head carries, and one head-scoped `kew epic unhold`
   receipt releases it. A held PR, or one waiting for a browser check, is
   skipped: the pass goes on to the PRs queued behind it. It runs `kew
   verify` on `[verify] host` over the candidate, telling it the fetched main
   as its base. When a queued PR's review could not run the full suite, that
   verify runs the full suite even under `[verify] suite = "tiered"`. Queue admission stores each PR's **Behavior changes**
   declarations. A later declaration edit drops the entry and raises
   attention. An entry with declarations always verifies alone. Its exceptions
   never apply to another PR. The verify also runs every queued PR's current
   acceptance contract. No declaration can excuse a contract failure;
3. before each merge, reads main and the PR again. Main must be the main the
   train was verified on, then each merge this pass made, and the PR still
   open at its queued head on the configured base. If main moved before the
   first merge, nothing merges and the entries go back to their places; the
   second time this happens to an entry, it is dropped with the reason. If main
   moved part-way through a train, the train stops and the rest is verified
   again. The acceptance contract ids and versions must also match the verified
   versions. An amendment requeues the remaining train for verification;
4. claims the entry, squash-merges the PR through the GitHub REST API bound
   to its queued head, and requires GitHub's answer to say it merged. It then
   stores a non-terminal landed record before validation. It then checks that
   main holds exactly the tree that was verified. It then confirms the durable
   mutation-report queue write before recording the entry as merged. A failed
   report write leaves the entry landed. A restarted runner retries the write.
   If GitHub reports the PR merged without a full merge commit SHA, the entry
   stays claimed. A later pass waits for the SHA, then performs the same tree
   validation and durable report write.
   A merge that landed any other tree stops the train and raises attention
   (`merge_queue_attention`, on the audit chain and to `[alerting]`). A push
   rejected by GitHub's head-bound merge drops the old entry with requeue
   guidance instead of recording a failed verify.

A PR that landed the verified tree is owed a
[diff-scoped mutation report](verify.md#diff-scoped-mutation-report) of its own
squash, against the main it merged onto (`mutation_report_queued`). The drive
runs owed reports after each merge pass, as it does for the standing rule's
merges ([Merging](drive.md#merging)); `kew merge queue run` merges only, and
leaves its reports owed for the next `kew drive` pass.

A failing verify fails the PR with the reason and posts it on the PR. The
ledger records that comment as pending in the same transaction as the terminal
verdict. The next pass retries a failed delivery, including after a restart.
An unpredictable marker makes the retry idempotent if GitHub accepted an
earlier request but its response was lost. In a train of several PRs, the queue
bisects to find the first PR whose prefix of the train fails, fails that one,
and verifies the rest again. A PR that does not merge cleanly onto main and the
PRs queued before it is failed the same way. Every verify of a bisection is
built on the same fetched main, and each failure names it.

**One window stays open.** Main can move between the queue's last read of
main and GitHub's merge. GitHub's merge API binds only the PR's head, not the
main it merges onto, so a merge that lands in that moment cannot be refused.
It is caught afterwards instead: the tree main holds after the merge is
compared with the tree that was verified, and a difference stops the train and
raises `merge_queue_attention`. The next pass then verifies main before
anything else merges.

**A train of more than one PR publishes unverified prefixes.** A train
verifies only its final tree, not each prefix of it, and its PRs merge one at
a time. So with `train` above `1`, main briefly holds a prefix of the train
(the first PR alone, then the first two, and so on) that was never verified on
its own. If a later merge in the train is then refused, main is left holding
that prefix, and it can be broken: PR 1 might break main and PR 2 fix it, and
PR 2 never landed. Main stays that way until the next pass verifies it. After
a train that stopped part-way (a refused merge, a main that moved between
merges, or a pass that died while merging), the next pass verifies main itself
before it verifies any PR. While main is red the queue merges nothing and
raises `merge_queue_attention` once; it verifies main again when main moves.

A stopped queue is also a needs-you item in the console of the project it
belongs to — the header count, Work and Monitor — with its reason and how long
it has been stopped, until a pass verifies main green. The item and
`kew merge queue list` say what is known about main: not yet verified, a merge
that landed a tree nobody verified (with the mismatch), or verified red (with
the failed verify). The failure is kept with the stop, so it is still there
after the one-off attention event has gone. Repair main, then
`kew merge queue run` verifies it and resumes the queue, even when the PR that
stopped it was the last one queued.

`kew serve` finds its project's queue the way `kew merge queue` does: when
`project.repo` is unset it detects the repo with `gh`. If detection fails, the
console shows every stopped queue in the ledger, each naming the checkout to
run its commands in, and marks the merge-queue source unavailable rather than
empty.

The default `train = 1` has no such window: every merge lands exactly the tree
that was verified for it.

**Every attention says why.** Each attention event carries a `reason` code,
and the console words the row from it:

| `reason` | Event | What happened, and the move |
|---|---|---|
| `main_red` | `merge_queue_attention` | Main verified red; fix main |
| `main_unverified` | `merge_queue_attention` | A merge landed a tree no verify read; check main |
| `declaration_edited` | `merge_queue_attention` | A queued PR's declaration changed after enqueue; it was dropped, so re-review it and add it again |
| `head_held` | `merge_queue_attention` | The pre-verify gate held a head until a person releases it |
| `contract_amended` | `merge_queue_notice` | An acceptance contract changed while verifying; the queue requeued the train itself |
| `review_withdrawn` | `merge_queue_notice` | A review round withdrew a verified head just before its merge; it was dropped and the rest of the train requeued. The round already sent the node back to its fix loop |

Each event also names its `repo` and, when it has one, its PR (`pr_number`),
and the console row leads with both.

Only `merge_queue_attention` asks for a person. It is magenta in the console and
an `error` to `[alerting]`. A `merge_queue_notice` is history the queue is
already handling, so it is neutral and goes out as `info`.

A main held red or unverified also counts in **needs you** (the header,
Monitor's holds and Work) for as long as the hold stands, whatever the audit
stream has scrolled past. Fix main, then run `kew merge queue run`, which
verifies main before anything else merges, even with nothing queued. The
command acts on the configured `project.repo`, so only that repository's row
offers it. When several projects share one ledger, another repository's held
main still shows, but its row tells you to run the command from a checkout
configured for it.

A head dropped for an edited declaration is refused by `kew merge queue add`
until a kew review round recorded after the drop finds it READY, so the edited
**Behavior changes** are never verified on the old review's authority.

The queue lives in the ledger, so it survives a reboot or a restarted drive:
the next pass picks it up where it stopped.

Exit codes follow the [exit-code scheme](exit-codes.md).
Removing a missing entry returns `1`. A held queue pass returns `2`.

**Example:**

```bash
kew merge queue add 2814 --head 3f1c0e9d6a4b2c8e7f5a1d0b9c8e7f6a5b4c3d2e
kew merge queue list
kew merge queue run       # or let kew drive take it
kew merge queue remove 2814
```

## Holding a PR head by hand

`kew verify --pr` holds the PR head it tests by itself (see
[Holding the PR head](verify.md#holding-the-pr-head)). `kew merge claim` takes,
renews and releases the same claim for a verify you run some other way, so the
drive pushes nothing onto the head meanwhile: no remediation, no merge of
main, no ci-fix.

```text
Usage: kew merge claim [OPTIONS] COMMAND [ARGS]...

Commands:
  take     Claim a PR head for 15 minutes; renew it every 5 while you verify.
  renew    Extend your live claim by another 15 minutes.
  release  Give the head back to the drive.
  list     Show every live merge claim.
```

```text
Usage: kew merge claim take [OPTIONS] PR

Arguments:
  PR  The pull request number.

Options:
  --head TEXT   The PR head commit, all 40 hex digits.  [required]
  --owner TEXT  Who holds the claim; defaults to KEW_SESSION or user:pid.
```

```text
Usage: kew merge claim renew [OPTIONS] PR

Arguments:
  PR  The pull request number.

Options:
  --head TEXT   The PR head commit, all 40 hex digits.  [required]
  --owner TEXT  The owner take printed; required unless KEW_SESSION is set.
```

```text
Usage: kew merge claim release [OPTIONS] PR

Arguments:
  PR  The pull request number.

Options:
  --head TEXT   The PR head commit, all 40 hex digits.  [required]
  --owner TEXT  The owner take printed; required unless KEW_SESSION is set.
```

```text
Usage: kew merge claim list [OPTIONS]

  Show every live merge claim.
```

A claim lives 15 minutes from its last `take` or `renew`; renew it every 5
minutes for as long as you verify, then `release` it. `take` refuses a head
somebody already holds, naming who and until when. `renew` refuses a claim
that already lapsed: the drive may have remediated the head in the gap, so
take it again and re-check the head.

`renew` and `release` act only on the owner's own claim, and name it
explicitly: pass the `--owner` that `take` printed, or set `KEW_SESSION` and
run all three under it. Without either they refuse and change nothing. `take`
falls back to `user:pid` of the calling shell, and that pid differs under
`uv run kew` and in every new shell, so the fallback cannot name the claim
again.

**Example:**

```bash
kew merge claim take 3198 --head 3f1c0e9d6a4b2c8e7f5a1d0b9c8e7f6a5b4c3d2e --owner alice-verify
kew merge claim renew 3198 --head 3f1c0e9d6a4b2c8e7f5a1d0b9c8e7f6a5b4c3d2e --owner alice-verify
kew merge claim release 3198 --head 3f1c0e9d6a4b2c8e7f5a1d0b9c8e7f6a5b4c3d2e --owner alice-verify
```
