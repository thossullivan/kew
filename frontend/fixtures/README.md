# Fixture API mode (FUI-02)

A dev-only mirror of the `kew serve` read API, so the Factory UI artboards in
`../design/factory-ui/` can be checked against a running app without a live
kew, a real repo or a real spend.

```sh
npm run dev:fixtures                        # /api answered from factory/*.json
npm run shoot -- /epics/launch-fixes DEpic  # two PNGs at 1440x960 in ../.shots/
```

`shoot` drives the Chrome that is already installed (`$CHROME`, or a short
probe list) and writes `DEpic.app.png` beside `DEpic.ref.png`. Compare the two
for position (±2px), token colours, type size and weight, hairlines and status
shapes, and list every deviation in the PR.

## What the payloads say

`factory/*.json` reproduces the data the artboards draw: the `launch-fixes`
epic (20 nodes, 9 edges, 13 done, one blocked, one in review), item #2055 and
its WorkView, spend of $178.05/$250 today and $915.05/$1,500 this week over a
$600 file cap, and the holds. The event stream is the exception to "the
artboards' data": DMonitor's five events (`design/factory-ui/DMonitor.events.json`)
sit nine days before the set's epoch, so served on the one delta they fell
outside the live window over them. `audit.json` instead carries five events
from the two runs the set holds in the half hour before the epoch — #2055's
review round (9c41e2b7) and #2033's dispatch (7d3f0a16) — each inside its run's
own window, and the histogram is counted for the range a read asks about,
with the committed day of buckets as the factory's background before them
(#2847).
`sweep-2026-10` is the same twenty nodes with no edge between any two and
nobody waiting (#2543): the epic page draws an edge-less epic as a card grid,
not a canvas, and `ui-check` reads that grid off it. It is reached by URL only
(`/epics/sweep-2026-10`), not listed in `epics.json`.
Where an artboard prints a placeholder — `[last tick]` — the fixture is
**empty**, so a screenshot checks the UI's honest empty rendering rather than a
number nobody designed. Not every placeholder earns that, though: `[elapsed]`
is a static artboard's stand-in for a clock it has no reading of, and the two
in-flight runs used to be served with no `started_at` to match it — which is a
row the backend cannot store, and which also filed both under "Unknown date" on
`/runs`. They now carry the start times the ledger really holds (#2377).

Every run status here is a `run_store.RunStatus` word, never the epic node's
`failed` or the console's display-only `running`: `src/lib/fixtures.test.ts`
holds all of them to `run-status.ts`'s `BACKEND_RUN_STATUSES`, and
`kew/tests/test_factory_status_parity.py` holds that set to the Python enum. The
fixtures had drifted to both, so fixture mode drew a Runs page reading 100%
success with a failure in its own list.

A row the set calls **in flight** is anchored to `FIXTURE_EPOCH`, not to the
story around it. The set is served with one delta (`now - FIXTURE_EPOCH`), so a
start time borrowed from `runs.json`'s own nine-day-old cluster is served 8d23h
old: DMonitor draws "8d23h" in the Elapsed cells, and a pending row that old is
one the host reads as a dead host rather than as work in progress
(`PENDING_RUN_MAX_AGE_S` in `service/work.py`). `fixtures.test.ts` holds every
in-flight row inside that bound — against the payload's own `now` where it
states one, as the epic timelines do — and the parity module above holds the
bound itself to the Python constant. The in-flight run is committed 30m20s
before the epoch, so it is served as the 30m row DMonitor draws. DMonitor's
other row, #2055's governed review, is a FINISHED round here (#2842): the same
set serves #2055 as the merge decision, and the server drops an item with a run
executing on it from the needs-you set — so a live review beside that merge
needs-you was a pairing no server can serve. The round has returned, which is
why the merge is the operator's.

The same rule covers the wording beside such a row: an in-flight work view's
`exception.detail` and `safest_action.reason` are **derived**, built by
`_rule_in_flight` out of the execution dimension's own `run_status`, so neither
is a caption to edit by hand. The parity module formats both off that function's
source.

Those start times also fix a compose **order**. Every payload here is a capture
taken at its own instant — the pipeline and evidence reads for #2055 are
composed at 14:47:40, after its review round returned READY at 14:46:40 and the
merge verify failed at that head (3670e43f) a few seconds later — and a capture
must reflect every ledger row that had started by the instant it states. A work view's execution dimension is
not a choice: `_execution_dto` reads `facts.latest`, and `_snapshot_facts` fills
`runs` with **every** row on the item, a review round included. So the work
projection is composed at 14:33:02, ten seconds before #2055's review round
starts, and reads the implementation run as latest. `fleet.pending` is the same kind of reading — it is
`len(store.list_pending())`, which counts `pending` rows and counted none while
the fixtures said `running`. Three payloads serve that one count and each is
read at the instant it states: `dashboard.json` at its `as_of.runs` (one row has
started), `health.json` at its nine-day-old `checked_at` (neither has), and
`fleet-status.json`, two booleans and a count with no instant on it, as of the
epoch (one has; #2055's round has finished by then). The parity module holds all of them to `runs.json`, through
the composer's own functions.

#2055 tells ONE story on every surface (#3461): round 1 READY at 3670e43f, the
merge verify failed at that head, and the merge decision is the operator's. The
popover's freshness row is current at that head, the pipeline's review stage
and the ledger's verdict both read ready, and the run page's verdict names the
same head. A check about a stale review serves one through `page.route`
(`epic-popover-pinned.mjs`'s `staleReview`) rather than reading it here.
`health.json` has every routed image present, so no screen's header counts
"1 down" beside the run it shows working; the gallery's `/health` shot takes its
missing routed image from `scenarios/health-image-missing.json`. `/scorecard`
reads `report-models.json` and `report-outcomes.json`, counted off `runs.json`
the way the server counts, and `/loops` reads the one pursuing loop Health
counts (`loops.json` and its `loops-<id>*.json`). A run's audit page is paged
forward from its `after_seq` cursor, as the server pages it.

One row goes **beyond** the artboards on purpose: `dashboard.json` serves the
go-live round-cap hold on `SFD-229` beside the merge decision the artboards
draw. It is there for the only thing it alone carries — a command `kew.waits`
really computes (`kew epic unhold <epic> <item>`) — because the Do this block
and its Copy button are drawn only where a row HAS one, and a merge decision
serves none (the merge is made on the host). Without it the whole command
affordance was reachable from no committed payload, so no screenshot could
check it. `kew/tests/test_needs_you_commands.py` rebuilds that row from the
server, so nothing on it can drift into a value chosen for a screenshot.

Two consequences to expect in a shot. `/work` lists an eleventh row, an ENTRY
row for a wait neither the projection nor the ready queue composes. And the
first screen lists **two** waits rather than expanding the single one: the
empty pane expands a block only when exactly one thing needs a person, and the
committed set cannot hold one wait and both facts at once — the merge decision
is the row with no command, and the hold is the row with one. So the gallery
takes a third `/work` shot, `work-needs-you` (`/work?item=SFD-229`), which
selects the commanded wait and photographs the block with its `Do this` line
and Copy button. `src/lib/needs-you/block.test.ts` holds that shot to it, and
`fixtures.test.ts` holds every `/work?item=` shot to the three item reads a
selection takes, so no shot photographs a 404.

## The prompt catalog

`prompts.json` and the ten `prompts-<role>.json` files are the backend's own
output (#3019), not typed by hand: `kew.service.prompts.list_roles` and
`role_preview` over `kew.prompt_previews.PREVIEW_RENDERERS`, with no project
root, so every preview is the packaged base rendered over the fixed sample
item. Regenerate them the same way when a template or the reviewer artifact
changes; `fixtures.test.ts` holds every listed role to a preview with the same
source.

## Scenarios — one payload a shot swaps in

`scenarios/*.json` is for the other half of that: a surface whose shape is a
property of the **whole** payload rather than of one row in it, which no single
committed set can hold two readings of at once. A gallery shot names the files
it wants (`fixtures:` in `scripts/gallery.mjs`) and the browser is answered
from them for that shot alone; `npm run dev:fixtures` goes on serving the one
set in `factory/`.

There are four. The first is the empty Work pane. The pane expands the needs-you
block when exactly ONE thing needs a person, and the committed set has two —
because the world it draws really has two: #2055's PR is awaiting a human merge
decision, so the server composes a row for it (`_work_rows`, which emits one
for every projection view that is not quiet), and SFD-229 is held, so it
composes a second. `scenarios/dashboard-sole-wait.json` is that same payload
with go-live's hold released — the merge decision alone, which is a set the
server really serves — and `work-sole-wait` is the `/work` shot taken over it.
`fixtures.test.ts` types it, holds its one row to the row the committed set
serves, and asserts that the pane's expansion is reachable from it.

The second is the first run (#2515). The `/work` first-run card shows only
when there is no work and no run history, and the committed set never has
neither. `scenarios/*-first-run.json` serve an empty dashboard, an empty work
projection, no runs, and a `/api/readiness` reading built from `kew doctor`'s
own `CheckResult`s, with `allowlist-network` failing and carrying its real fix
command. The `work-first-run` shot and `ui-check` both draw over it; `ui-check`
also flips its `project-config` check to failing (#2652) to see `kew init`
become the card's one primary command.

The third is a held item between review rounds (#2842). The committed
projection does not compose SFD-229 at all: its row on Work is an entry row,
drawn from the go-live hold alone. `scenarios/dashboard-work-held-round-due.json`
composes it, with its last review round owing a full one (`review_round_due`,
"kew re-reviews next"), which is what live #2814 served while a
`base_integration_conflict` hold stopped the re-review. Served with the
committed dashboard, the row must read HELD with the needs-you diamond, never
WORKING. `ui-check` (`needs-you-working.mjs`) and `list.test.ts` both draw over it.

The fourth is a second host death under an epic node (#2964), clean and dirty.
`scenarios/dashboard-host-restart-repeated.json` holds the two rows, keyed
`clean` and `dirty`, exactly as the real `GET /api/dashboard` served them over
the ledger the driver wrote for each death: `kew/tests/test_host_restart_reap.py`
serves them again and fails on any field but `age` (the tick's clock). Nothing
in it is typed by hand, so a wrong command is caught in Python before
`ui-check` (`host-restart-repeated.mjs`) draws the row and copies its command.
`scenarios/*-launch-fixes-host-restart-repeated.json` put the clean case inside
the whole committed set (#2971): #2058 failed that way, with its row and its
node's attention as the server composes them, and `ui-check`
(`host-restart-repeated-surfaces.mjs`) draws it on Work, Monitor and the epic
popover.

The fifth is the same node a step later (#2960, #2988): its preserved
continuation died with the host too, so the node is failed with its work still
in the checkout, and the one command that recovers it is `kew epic continue`.
`scenarios/host-restart-preserved-continue.json` holds the needs-you row the
real `GET /api/dashboard` served and the node the real
`GET /api/epics/launch-fixes` served over that ledger;
`kew/tests/test_host_restart_preserved_continuation.py` serves both again and
fails on any field but the clock's stamps. `ui-check`
(`host-restart-preserved-continue.mjs`) adds both to the committed set and
reads Work, Monitor and the Epic page off them: the command and its Copy
control painted on each (`assertPainted`, and Copied again after the click),
the command exactly once in each Epic surface that carries it (the node popover
and the selected-node rail) and nowhere else on that page, and on the Epic
canvas the node's magenta needs-you card, whose click opens the popover
carrying it.

## Adding a read

The route mapping is mechanical: the path after `/api/` becomes the file name
with `/` turned into `-`. `/api/report/spend` reads `report-spend.json`;
`/api/epics/launch-fixes/nodes/2055` reads
`epics-launch-fixes-nodes-2055.json`. A `:` in a segment reads as `_`, because
one id the server mints carries one: a sealed artifact's node is
`<run>:<through_seq>`, so `/api/runs/f5dcb6a5/nodes/f5dcb6a5:4/diff` reads
`runs-f5dcb6a5-nodes-f5dcb6a5_4-diff.json`. A path with no fixture answers 404 naming
the file it wanted. Add the file, then add its route and its typed import to
`../src/lib/fixtures.test.ts` — the test fails on a fixture that is unrouted,
untyped, or orphaned.

## Why none of this ships

`fixtureApi()` returns `false` unless `KEW_UI_FIXTURES=1`, it is `apply:
'serve'`, and this directory is outside both `src/` and `static/`, so nothing
imports it and no build copies it. `fixtures.test.ts` pins all three and greps
a build output for a fixture-only string whenever one is present.
