# The demo console

[demo.kew.dev](https://demo.kew.dev/) is the web console that
[`kew serve`](index.md) shows, running in your browser with nothing behind it.
It shows how kew builds software: agents work headless and sandboxed, kew
checks what they produce, and a person makes each merge decision. kew itself
is built this way.

## What you are looking at

- **Synthetic data.** The epics, work items, runs and costs in the demo are
  made up. No real epic, item, run, path or name appears in them.
- **Build history.** The **Build history** line at the foot of a page is
  there to read kew's own history: the pull requests, agent runs, review rounds and
  list-price cost that built that page. Until that history is exported to the
  published demo, the line reads sample data and is labelled so.
- **Allowlisted fields.** Build history publishes only those counts and costs,
  each feature's id, title, routes, epics and issue numbers, its pull requests
  and the commits they landed, its held-out failure counts, its flagged-run
  counts by flag and where they came from, and the time of the export. No log, prompt, error text
  or other free text is published.
- **Nothing happens.** The demo has no backend. A button that would change
  kew answers with one sentence: what it would have done and, where there is
  one, the `kew` command that does it.

## Tours

A tour walks you through the console one step at a time. Each step's text is a
passage from these docs, and every such passage carries a
**See it in the demo** link that opens the tour on its step.

Tours run on a desktop window, 1024 px wide or wider. On a narrower window, a
tour link opens its page without the tour. Open a tour from the console's
**More** menu, or start it here:

| Tour | What it shows |
|---|---|
| [What needs me, and what do I run?](https://demo.kew.dev/work?tour=needs-you&step=1) | What needs you, the work behind the count, holds and merge decisions, and an epic's heartbeat. |
| [Follow an epic from its first tick](https://demo.kew.dev/epics/ledger-v2?tour=follow-an-epic&step=1) | An epic's replay, from its first tick to its last frame: merges, nodes in flight, a review round and what it spent. |
| [Why was this PR refused?](https://demo.kew.dev/runs/5cad8a97?tour=refused-pr&step=1) | The gates that refuse an agent's pull request, and why. |
| [Where did the money go?](https://demo.kew.dev/runs/1b464391?tour=where-did-the-money-go&step=1) | Spend followed from a run to its node, its epic and the budget. |
