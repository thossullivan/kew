# kew finalize

Repair a run left awaiting host publication after out-of-band publication.

```text
Usage: kew finalize [OPTIONS] RUN_ID
```

Use `kew finalize <run-id>` only for residue that is still recorded as
`awaiting_publication` after publication was completed out of band. The command
atomically marks the run successful and terminals its unclaimed preserved-work
continuation, then records the operator intent in the chained audit ledger. It
refuses missing runs and every run state other than `awaiting_publication`.

It also records the branch's verified remote head as this run's published
output, so a governed remediation (a review round, `kew resolve`, `kew ci-fix`)
can bind a head the operator published by hand. An unreachable origin never
blocks the repair: no head is named, and the finalize event records why rather
than reading as "this run published nothing". Neither does a head another run
already claims — finalizing a second run stranded on the same branch leaves the
first run's claim standing and records why it named no head, because a second
claim on one head would refuse the branch as ambiguous for good. For the
operator's *later* commits on that branch, see
[`kew publication attest`](publication.md).

Normal recovery does not require this command: each epic-driver tick discovers
an open or merged PR from the preserved branch, reconciles the run, and closes
the Linear item after an externally merged PR. The repair verb is the explicit
escape hatch for older or otherwise unreconciled rows.

The tick's reconciliation settles the run but never claims its head: only a
human claims one. So `kew finalize` also accepts a run whose settlement was a
`publication_reconciled` event and that has no head claim yet. The run is
already `success`, so the command only claims the branch's verified remote head
for it, under your identity. Claiming the head is the whole repair here, so if
no head could be claimed (an unreachable origin, a head another run holds) the
command refuses and says why, and you can run it again. Once the head is
claimed, the run is no longer finalizable.

**Example:**

```bash
kew finalize <run-id>
```

## Exit status

See the shared [exit-code scheme](exit-codes.md).

| Code | Meaning |
| --- | --- |
| `0` | Run finalized, or a reconciled run's head claimed. |
| `1` | The run state changed underneath the command before the finalization could commit. |
| `2` | Unknown run; a run in any state other than `awaiting_publication` that is not a reconciled run with no head claim; a reconciled run whose head could not be claimed; missing run id; configuration failure. |
