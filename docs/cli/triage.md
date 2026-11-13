# kew triage

```text
Usage: kew triage COMMAND [ARGS]...
```

Classify a work item and record the proposal; a person decides.

The triage station reads one item, asks a classifier what it is, and writes the
answer to the ledger. **In this release every outcome is a proposal.** The
station applies nothing to the tracker: no label, no comment, no `ready`, no
close — not even when it proposes a dispatch, and not when the issue body asks
it to. A model may classify, propose and draft; dispatching, closing and
accepting stay human acts.

It is off by default. Turn it on with `[triage] enabled = true` and point
`[triage] model` at a model your OpenAI-compatible endpoint serves (see
[`[triage]`](../configuration.md#triage)).

## `kew triage <item>`

```text
Usage: kew triage ITEM

Arguments:
  ITEM        GitHub issue number (2293) (required)
```

Reads the item through `gh issue view`, computes the host-side signals, asks the
classifier, applies the policy, and records one `triage_decisions` row keyed on
a digest of the body it read. Re-running it on an unchanged body records nothing
new; an edited body supersedes the previous proposal, which stays in the
history.

What the host computes itself, rather than trusting the model for:

- **duplicate candidates** — `gh search issues` over open and closed issues for
  the item's title terms plus the error strings its body quotes, capped at
  `[triage] dedupe_candidates`. The model may only name a `duplicate_of` from
  that list; a number it invents is dropped.
- **body features** — whether the body states reproduction steps, an
  expected/actual pair, a version string or a stack trace, and how long it is.
- **the labels already on the item**, and the body digest.

The item's title, body and the candidate titles reach the model fenced between
two copies of a per-render random sentinel, as untrusted data.

**Example:**

```bash
kew triage 2293
```

## `kew triage queue`

```text
Usage: kew triage queue [OPTIONS]

Options:
  --json  Print one JSON document instead of text.
```

Prints every proposal no person has decided yet, oldest first, each with the
command that decides it. Read-only — it opens the ledger read-only, so it cannot
migrate one.

`--json` prints the same proposals, in the same order, as one JSON document on
stdout. An empty queue is an empty `proposals` list; a missing or unreadable
ledger is still refused, with no document and the same exit status as without
`--json` (see [Exit codes](exit-codes.md)).

```json
{
  "schema": 1,
  "proposals": [
    {"item_id": "2293", "issue_num": 2293, "outcome": "propose_dispatch",
     "type": "feature", "priority": "p2", "complexity": "medium",
     "confidence": 0.86, "actionable": true, "needs_info": false,
     "duplicate_of": null, "backend": "local", "model": "qwen2.5-7b",
     "recorded_at": "2026-10-10T09:00:00+00:00",
     "decide": "kew triage decide 2293"}
  ]
}
```

## `kew triage decide`

```text
Usage: kew triage decide [OPTIONS] ITEM

Arguments:
  ITEM                    Work item whose proposal you are deciding (required)

Options:
  --dispatch              You will dispatch this item.
  --ask                   You asked the item's author for more.
  --close-duplicate N     You closed this item as a duplicate of issue N.
  --decline               You declined this item.
  --by TEXT               Who is deciding (recorded on the audit chain;
                          defaults to the local user).
```

Records **your** decision, with your name, as `triage_resolved` on the audit
chain. Exactly one of the four flags is required: a decision with none and a
decision with two are both ambiguous, and recording either would put a
resolution on the chain that nobody chose.

This command records the decision; it does not perform it. Dispatching the item
is still [`kew dispatch`](dispatch.md), and closing it is still yours to do — a
station that closed an issue off its own proposal would be the station deciding.

**Example:**

```bash
kew triage queue
kew triage decide 2293 --dispatch
kew triage decide 2210 --close-duplicate 812 --by alice
```

## Outcomes

| Outcome | When | What it asks of you |
| --- | --- | --- |
| `propose_dispatch` | actionable, confident, not a duplicate | dispatch it, or decline |
| `ask` | `needs_info`, or questions with nothing to work on yet | ask the author |
| `duplicate_candidate` | `duplicate_of` names a candidate the host found | close it as a duplicate, or decline |
| `human_decision` | low confidence, a `[triage] human_types` type (`security`, `question`), or nothing actionable | decide it yourself |
| `split` | actionable but `complex` | split it into more than one item |
| `unavailable` | the classifier was unreachable, or its JSON never validated after one repair retry | look at the item yourself |

With `[triage] skip_epic_nodes` (the default), an item an epic already planned
is left alone and nothing is classified: its plan came from the epic. A ledger
whose epic journal cannot be read triages the item and says so, because a skip
taken on no evidence is worse than a redundant proposal.

`unavailable` is the fail-closed outcome and it can never become a dispatch. It
is still recorded as a row, because "triage could not read this item" is a fact
and an absence would read as "nobody has triaged it yet".

Every unresolved proposal is one row in the console's needs-you set, carrying
the exact `kew triage decide` command for that item.

## Exit codes

See the shared [exit-code scheme](exit-codes.md).

| Code | Meaning |
| --- | --- |
| `0` | Proposal recorded; decision recorded; item skipped as an epic node; nothing waiting in the queue. |
| `1` | Triage unavailable — recorded as a proposal for a person. |
| `2` | Station disabled; non-numeric item; GitHub read failure; missing or unreadable ledger; no unresolved proposal for the item; ambiguous decision flags; unrecordable proposal; configuration failure. |
