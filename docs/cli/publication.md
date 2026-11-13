# kew publication

Operator evidence about heads published outside kew.

```text
Usage: kew publication [OPTIONS] COMMAND [ARGS]...

Commands:
  attest  Name an operator-pushed head as the published output of its producer.
```

## kew publication attest

```text
Usage: kew publication attest [OPTIONS] BRANCH SHA

Arguments:
  BRANCH  Branch whose head was published
  SHA     Full 40-character sha of that branch's remote head

Options:
  --item TEXT  Tracker item the branch's runs belong to  [required]
```

kew binds a governed remediation (a review round, `kew resolve`, `kew ci-fix`)
to the run that produced the branch head it is about to change. Normally that
binding comes from kew's own publication: the host publisher records what it is
about to push, so the head names its producer.

A head kew did not push carries no such record. When an owner approves output
held in `publication_review_required` and the operator publishes the files by
hand, or when an operator pushes a fix for a review finding, the branch head is
unclaimed — and every governed remediation on that branch refuses it with
`publication ancestry is ambiguous and protected output is not releasable`.

`kew publication attest` records the missing claim: this named human says this
remote head is the published output of the producer its parent already binds
to. It is deliberately narrow:

- The sha must be the branch's **current remote head**, read from the
  configured origin. A sha that is not the head is not what a remediation
  would check out.
- The head's **first-parent path must reach a commit that already binds to a
  producer**. Attestation extends a bound history; it never invents a producer
  for one. Only a recorded binding counts: kew's own publication job for that
  exact commit, or a head claim naming it. The branch's newest run is never
  assumed to have produced a commit nobody claimed.
- Only two commit shapes bind. A **single-parent commit** binds through that
  parent. A **two-parent merge** binds through its first parent, and only when
  its second parent is reachable from the freshly fetched base branch — the
  merge an operator makes by hand when a base conflict cannot be resolved
  automatically. An octopus merge, a merge of something that is not the base,
  and a merge made the other way round (the bound head as the *second* parent)
  are each refused by name. The base branch is `branches.base` from your
  config, or — when that is unset — the default branch of the repository the
  command reads, which is the repository you run it in, not whatever checkout
  your shell happens to be in.
- A **chain of up to 10 such commits** binds, not just the head's own parent.
  Merging the base into the branch and then fixing a test on top is two
  commits, and folding them into one needs a force push that operator hooks
  and branch rulesets commonly block — so the path from the head is walked,
  first parent by first parent, until it reaches a bound commit. Every commit
  on that path must be one of the two shapes above; one that is not refuses
  the whole chain and is named in the refusal, as is a path that runs past ten
  commits without reaching a bound one. A commit on the path that is itself
  **claimed** by a claim that does not bind — two claims holding one commit, or
  a producer whose publication credentials are refused — stops the walk as
  well: the path is never taken past it to bind an older producer. The claim
  records every commit it bound through, and the bound commit the chain
  reached.
- Exactly **one claim per head** still applies, so a head another run already
  claims is **refused, not claimed again**: a second claim would refuse the
  branch as ambiguous wherever the binding is read, and a chained claim cannot
  be withdrawn. Re-attesting the same head for the same producer under the
  same item is idempotent. The same producer's claim under another item is
  refused like any other run's, because the lookup would read two claims.

A branch can carry runs that worked on the PR opened from it, such as a
`kew ci-fix` or an older review row, and record the PR number as their item. The
branch then names two items: the node and its PR. kew still treats the node as
the branch's owner. A PR-subject run whose item is empty, or is the PR number it
records as its issue, served that PR and is set aside. So
`kew publication attest agent/issue-N <sha> --item N` is accepted on such a
branch. The head lookup reads a mixed branch under every item its runs name,
and the one-claim-per-head check reads the same scopes. A hand `kew review` of
that PR records its claim under N too. A run naming any other item still
refuses the attestation.

`kew finalize` records the head for the run it finalizes, so the first
hand-published head on a branch needs no separate attestation. Use
`kew publication attest` for the operator's *later* commits on that branch.

**Example:**

```bash
kew publication attest agent/issue-2067 3a50fc51... --item 2067
```

## Exit status

See the shared [exit-code scheme](exit-codes.md).

| Code | Meaning |
| --- | --- |
| `0` | The head is attested and bound to its producer. |
| `2` | The sha is not the remote head; no commit on the first-parent path binds to a producer, or the path runs past ten commits; a commit's parent shape is not bindable, or a merge's second parent is not on the base; a commit on the path is claimed by a claim that does not bind; the head is already claimed by another run; the branch's claims could not be read back under one tracker item; the remote, the commit or the base branch could not be read; malformed arguments; configuration failure. |
