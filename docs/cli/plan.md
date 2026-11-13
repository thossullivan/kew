# kew plan

```text
Usage: kew plan COMMAND [ARGS]...
```

Read the acceptance criteria kew recorded for a work item.

kew records an item's plan as the dispatch launches, before the prompt is
built: it reads the issue body it already fetched, extracts the body's own
acceptance section, and writes the criteria to the ledger keyed on a digest of
that body, bound to the run that read it. Dispatching the same body again
records nothing new; an edited body supersedes the previous row, which stays in
the history.

`kew plan show` reads that record back. It never re-reads the live issue, so
what it prints is what the item was dispatched against — not what the body says
now.

The extractor takes the first `## Acceptance` or `## Acceptance Criteria`
heading (through `####`), reads its bullets, numbered items and checkboxes up to
the next heading of the same or higher level, and keeps every statement
verbatim as `AC1`..`ACn`. A criterion that wraps onto further lines is one
statement: the continuation lines are folded in, because half a criterion is
not the contract the item stated. Headings and list items inside fenced code
blocks are ignored. A body with no such section records a plan with zero
criteria — which is a different fact from an item with no plan at all, and the
command says which it found.

```text
Usage: kew plan show [OPTIONS] ITEM

Arguments:
  ITEM        GitHub issue number (2278) or tracker id (SFD-12) (required)

Options:
  --history   Also list the superseded plans, oldest first
```

**Example:**

```bash
# The plan this item was dispatched against
kew plan show 2278

# Every plan recorded for it, oldest first
kew plan show 2278 --history
```

Each plan prints its sha, where it came from (`extracted`, `model` or `human`),
how many criteria it carries, when it was recorded, who accepted it, and the
digest of the body it was read from. The dispatch prompt names the sha of the
row kew wrote for that dispatch, so a prompt and a ledger row can always be
matched up -- and when the write failed, the prompt names no sha at all rather
than one this command would report as unrecorded.

Accepting a plan stays a human act: an extracted plan has no `approved_by`, and
nothing in kew grants one.

## Exit codes

See the shared [exit-code scheme](exit-codes.md). A ledger older than recorded
plans counts as unreadable here: the read-only open this command uses cannot
migrate it, so the command says it cannot read plans from that ledger instead of
reporting the item as having none.

| Code | Meaning |
| --- | --- |
| `0` | Plan printed. |
| `2` | Missing ledger; unreadable ledger; no plan recorded for the item; unreadable stored plan; configuration failure. |
