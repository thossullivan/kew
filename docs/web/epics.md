# Epic DAG live view

`kew serve` renders the epic driver's own record as a **live, replayable
dependency graph** — the flight-recorder model applied to `kew epic
run`/`advance` instead of a single dispatch. Two pages:

- **`/epics`** — a list of every epic the driver has journaled at least one
  tick for: ref, tri-state signal, done/failed counts, drain spend, and how
  long ago the last tick landed. This is navigation, not a dashboard — one
  epic per page, no fleet-wide composite view.
- **`/epics/<ref>`** — the graph for one epic, in **live** mode while a
  driver is running it, or **replay** mode over a completed drain's full
  journal history.

Both pages are read-only, like the [Runs view](index.md#runs):
no dispatch, merge, retry, or stop affordance lives here. A node held for a spend approval
points to Monitor's holds column, where the approval is decided; a FAILED node's pane shows the
`kew epic retry <node>` command rather than a button that would run it.

## What feeds the page: the tick journal

Everything rendered here — signal, node statuses, counts, frontier, drain
spend — is read from the `epic_ticks`/`epic_driver_state` tables the driver
itself journals every tick (see [`kew epic`: Tick journal](../cli/epic.md#tick-journal)).
The page never re-derives status from GitHub on its own; it shows the
driver's own record, so there is no way for "what the driver did" and "what
the page shows" to disagree. An epic the driver has never ticked shows an
honest empty state rather than a fabricated graph.

## The honesty model: two layers, updated differently

The page draws two visually distinct layers on top of the same graph, and
they are **not** on the same clock:

- **Status colors** (a node's fill, the frontier ring, HELD/FAILED
  treatment) change **only on journal frames** — i.e. only when the driver
  itself observed and recorded a transition. Every status-colored pixel
  traces to a journaled, audited record.
- **The live-telemetry layer** (an activity dot on IN_PROGRESS nodes, the
  in-flight cost readout) updates on the SSE poll cadence, independent of
  whether the driver has journaled anything new. It is visually distinct
  from status color and labeled as live telemetry, not as a driver
  observation.

Tick cadence moves the graph; run cadence moves the money. Between
transitions you see proof of life — the telemetry layer, the "tick N ·
change M · Xs ago" header — never fake motion invented to make the page look
busier than the driver actually is.

## The tick strip

`change M` in the header counts **journal rows**, and the journal is a change
journal: a tick that observed nothing new is deduplicated away. In practice
roughly nine ticks in ten leave no row at all — one drain ran 476 ticks and
journaled 18 — and the ticks that never reach the journal (one that raised, one
locked out by another driver of the same epic, one whose tracker query matched
no work) reached nothing but the driver's stderr.

So the driver also records one row per tick **attempt**, in `epic_tick_log`:
when it started, when it finished, what it did (`dispatched` / `merged` /
`held` / `nothing`) or why it did not (`failed` with the exception's class,
`locked_out`, `empty_graph`), and the per-tick counts. Only the exception's
**class** is recorded — never its message, which can carry a checkout path or a
credential-bearing URL into a row the console shows.

The table is capped per epic, so it is the recent past rather than a ledger;
old rows are dropped, never rewritten. A row written by an older build keeps
whatever it says — a reading the console has no mark for draws quiet.

A tick with no finish stamp is a tick still running, and that is the only "live"
state there is to go stale. A driver killed mid-tick — TaskStop, an OOM kill, a
host reboot — closes nothing, so the next tick to hold that epic's driver lock
closes the row it left behind as `abandoned` (nobody can say what that attempt
did, so it draws quiet). An epic whose driver never comes back has no next tick:
there the strip itself stops reading the row as live once it is older than the
driver-liveness threshold, which is the same age at which another driver may
steal the epic's lock.

Both surfaces read it from `GET /api/epics/<ref>/tick-strip?limit=` (bounded in
SQL, no GitHub call), never from `/ticks`. Neither polls it on a timer: a strip
can only change when the driver attempts a tick, so both re-read it when the
epic's attempt stamp moves — which it does for every attempt, including the
ones that never got past the lock, the preflight or an empty graph:

- **A compact change strip per row** on the Epics list, in fixed slots so the
  strips align down the column. It draws the epic's recent **changes** — the
  attempts that journaled a change, which is what the Change column counts;
  one dot per change, though every unchanged attempt after it points at the
  same journal row — as grey dots, plus a failed tick as the red cross and the one tick still
  running as the only cyan slot. Ticks that changed nothing are left out, so
  the strip moves when Change moves. It reads
  `GET /api/epics/<ref>/tick-strip?changes=true`, which filters in SQL, so a
  change is found however many no-op ticks follow it (as far back as the kept
  log). The Ticks heading's tooltip says what each mark is.
- **A full strip** on the epic's own page, placed on the shared time axis under
  the header and batched where attempts overlap — so a dense minute is one mark
  that says how many it stands for and wears its worst member's state, with the
  counts and the error class in its popover. A batch holding the running tick
  grows, but it is only cyan when there is nothing in it to act on: a failure
  batched with a live attempt still draws as the failure. The page asks for as
  many attempts as the lane can draw apart and no more, so the strip cannot
  collapse into a single bar.

The loops page draws the same strip per objective from
`GET /api/loops/<id>/iterations?limit=`, which is the bounded tail read: a row's
single status mark says where a loop *is*, and the strip beside it says how it
got there.

## Live vs. replay

- **Live** subscribes to `GET /api/epics/<ref>/stream` (SSE): the server
  fans out `snapshot` / `economics` / `heartbeat` frames from a shared poller
  reading `dispatch.db`, at most one poll per second per driven epic
  regardless of how many browser tabs are open. A reconnect (including the
  server's periodic stream cap) always re-sends the latest frames rather
  than replaying history — the live view never backfills missed
  intermediate actions; only replay does that.
- **Replay** fetches the full journal via `GET /api/epics/<ref>/ticks` and
  steps through it locally. Pacing is **frame-indexed, not wall-clock**:
  changed-only journal frames are wildly irregular (a multi-hour HELD
  plateau, then a merge cascade), so linear time-compression would render
  most of a drain as dead air. The default preset dwells roughly
  `30s / frame_count` per frame (clamped to a sane per-frame range). The
  **wall clock is mandatory UI** in replay — each frame shows its `tick_ts`
  and elapsed time since the drain started, and the scrubber's tick marks
  are spaced by real elapsed time, not by frame index, so a clock jump
  between two adjacent frames is visible rather than hidden behind evenly
  spaced marks. A **1x / 10x / 60x** control beside the scrubber multiplies
  that pace.

A replay is a link: `/epics/<ref>?mode=replay&t=<seconds>&speed=<1|10|60>`.
`t` is whole seconds since the journal's first tick, and the link opens
paused on the last frame recorded at or before it, so the same `t` always
draws the same graph (`t=0` is the first frame). Pausing, scrubbing (when
you let go of the scrubber) and the speed control rewrite the link, and
selecting a node or switching views keeps `t` and `speed`. Without `t`, the
replay plays from the first frame. Following a replay link to the epic
already on screen moves the replay to its moment. A replay link to an epic
whose journal has no recorded change yet says so in the header and keeps the
link.

### Following an epic through its replay

The demo console's **Follow an epic** tour opens every step as a replay link,
so a step always shows the same moment of the same epic.

The demo's epic has 13 nodes, numbered #101 to #113; each step names the card
it is about.

<!-- demo-step: follow-an-epic/1 -->
**Agents build a feature**

Watch agents build a 13-issue feature, every change reviewed before it merges.
The replay opens on the first tick: #101 reads in progress, dispatched to its
own sandboxed agent. The other cards wait their turn.
<!-- /demo-step -->

[See it in the demo](https://demo.kew.dev/epics/ledger-v2?tour=follow-an-epic&step=1)

<!-- demo-step: follow-an-epic/2 -->
**A merge**

Two frames later #101 reads done: its pull request merged after review. The
count above the graph now reads 1 of 13 done.
<!-- /demo-step -->

[See it in the demo](https://demo.kew.dev/epics/ledger-v2?tour=follow-an-epic&step=2)

<!-- demo-step: follow-an-epic/3 -->
**In review**

#107 reads in review: its pull request is open, and it goes to kew's reviewer,
a fresh agent in its own sandbox that did not write the change.
<!-- /demo-step -->

[See it in the demo](https://demo.kew.dev/epics/ledger-v2?tour=follow-an-epic&step=3)

kew dispatches nodes whose dependencies are done, each to its own sandboxed
agent, up to [`[fleet] max_concurrent`](../configuration.md#fleet) at once
(it falls back to `[parallel] max_workers`, one at a time by default). The
header counts the nodes whose dependencies are done and that have not started
as next up: on this frame, #106 and #113.

<!-- demo-step: follow-an-epic/4 -->
**Sent back**

One frame later #107 reads in progress again. The reviewer did not pass the
pull request, so kew sent the node back to an agent to fix, and the next round
reviews the new head.
<!-- /demo-step -->

[See it in the demo](https://demo.kew.dev/epics/ledger-v2?tour=follow-an-epic&step=4)

<!-- demo-step: follow-an-epic/5 -->
**What it spent**

On the last frame each card's dollar figure is what its node's runs cost since
the epic's first tick, at list price. #107 went back through review, so it
carries every round's runs and reads the most.
<!-- /demo-step -->

[See it in the demo](https://demo.kew.dev/epics/ledger-v2?tour=follow-an-epic&step=5)

## Graph or Timeline

The canvas carries a **Graph / Timeline** switch at its top left (`T` throws
it). Both views read the same epic and share the same selected node, so a node
picked on one is still picked on the other, and the URL carries the view
(`/epics/<ref>?view=timeline&node=2219`) — a link lands on the view it was
copied from.

- **Graph** is the dependency DAG described above.
- **Timeline** answers "where did the time on this epic go?": one row per node,
  oldest first activity at the top, against a time axis. Behind each row is the
  status the driver recorded — cyan while kew was working, magenta with a left
  edge wherever a person was the next move. On it are the runs (grey for the
  implementation, cyan for a review, amber for a remediation pass, outlined red
  if the run failed or was killed), then a dot per review round — green for
  READY, amber for anything else — and a green dot where the PR merged. A run
  or a status still open is drawn out to the now line, which the view re-reads
  every 15 seconds until the epic is `complete`.

The wheel zooms the time axis about the pointer, a drag pans it, `F` fits the
whole span, and hovering a bar names its kind, status, span, duration and cost.
Everything it draws comes from `GET /api/epics/<ref>/timeline` — the same tick
journal and run history the graph reads, not a second source.

## Drain-spend semantics

The header's spend figure and each node's cost badge are **drain-scoped**,
not lifetime: they sum `runs` rows with `finished_at` on or after the epic's
**first journal row**, so a node's cost badge doesn't inflate with unrelated
manual dispatches from before the drain started. Replay frames apply the
same window, upper-bounded at each frame's own `tick_ts` — so the first
replay frame's spend reads near zero, as it honestly should, and the last
frame matches the epic's true total drain spend.

**Lifetime** issue spend (all runs ever recorded against that issue number,
drain or not) is shown only in the node detail pane, explicitly labeled as
lifetime — never conflated with the drain-window headline number.

### Following the money from a run to its epic

Every dollar figure on these pages is what kew records for its runs: the
API-equivalent list price of the tokens each run used. It is not a bill. On a subscription plan the same tokens are part of what the plan
costs. The figure on a node's graph card and an epic's figure are drain
spend: the runs that finished since the epic's first tick. A node's drain
figure is the sum of its runs in the drain, and an epic's is the sum of its
nodes' figures, so any amount can be traced back to the runs that spent it.
A node's popover also states its lifetime spend, every run recorded against
it, which can be more than its drain figure: a run from before the drain
counts there and not on the card.

<!-- demo-step: where-did-the-money-go/1 -->
**One run's cost**

A run's page leads its Cost section with what that one run cost: the
API-equivalent list price of the tokens it used, not what was billed.
<!-- /demo-step -->

[See it in the demo](https://demo.kew.dev/runs/1b464391?tour=where-did-the-money-go&step=1)

<!-- demo-step: where-did-the-money-go/2 -->
**A node's runs**

The popover of the node that run worked on lists its latest runs, each at list
price. The line above them is the node's lifetime spend: how many runs and
what they cost in all.
<!-- /demo-step -->

[See it in the demo](https://demo.kew.dev/epics/ledger-v2?tour=where-did-the-money-go&step=2)

<!-- demo-step: where-did-the-money-go/4 -->
**The epic's spend**

On the Epics list, an epic's figure is its drain spend: the list price of its
nodes' runs since its first tick. It is the sum of its node cards, each the
sum of its runs.
<!-- /demo-step -->

[See it in the demo](https://demo.kew.dev/epics?tour=where-did-the-money-go&step=4)

## Staleness, complete, and stalled banners

The header banner is signal-aware and mode-aware so it can't lie in either
direction:

| Situation | Banner |
|---|---|
| Driver alive, ticking normally | Advancing "tick N · change M · Xs ago" counter, no judgment |
| Driver process gone mid-drain | "driver last seen …" staleness banner once the tick-age threshold is exceeded |
| Epic `complete` | "COMPLETE · finished `<ts>`" — **never** a stale banner, even if the page hasn't been reloaded in hours |
| Epic `stalled` | A stall banner plus the stuck nodes' reasons — distinct from "driver gone," since a stalled driver exited by design |
| One-shot/cron driver (`interval_s` is null) | "last driven Xs ago (one-shot driver)" — no alive/gone claim is made, because none is possible |
| Replay mode | The live staleness header is replaced entirely by the frame's own wall clock |

## Auth under `--expose`

The DAG page has no login UI of its own. It reuses the same session cookie
the [sign-in link](index.md#signing-in) mints from the `/#token=…`
flow:

- On loopback without `--expose`, reads work ungated, matching the rest of
  the read surface.
- Under `--expose`, every `/api/epics*` route requires a valid session (the
  router-level `read_guard` pattern — no endpoint on this router can be
  added without it).
- Any 401 redirects the browser to `/inbox`, which handles both a fresh
  `#token=` fragment and an existing session, then returns.
- **No epics route ever accepts a token as a query or fragment parameter.**
  The token only ever travels once, in the `/inbox` fragment, exactly like
  every other authenticated surface in kew.

See [Security: Approval inbox](../security.md#approval-inbox) and
[`kew serve`: Authentication](../cli/serve.md#authentication) for the full
model this page inherits.

## Recording a demo

The page is its own capture pipeline — no export code, just a screen
recorder over real infrastructure. Two scripts (run from the `kew/` package
dir) reproduce the canonical scripted drain end-to-end:

```bash
# 1. Generate a completed demo drain (9 journaled ticks, HELD + FAILED beats)
uv run python scripts/make_demo_epic.py --out /tmp/demo-src

# 2. Point a scratch project's [logging].dir at an empty logs dir, start
#    `kew serve` there, and open /epics in a browser.

# 3a. Replay capture: copy /tmp/demo-src/dispatch.db into the logs dir,
#     open /epics/demo-epic, switch to replay, press play, record.

# 3b. Live capture: leave the logs dir empty and perform the drain in
#     real time instead — statuses advance on screen as rows land:
uv run python scripts/drip_demo_epic.py /tmp/demo-src/dispatch.db \
    <scratch-project>/logs/dispatch.db --interval 6
```

The drip script shifts every timestamp so the drain reads as happening now
(fresh heartbeat, live-accumulating drain spend) and recomputes each
snapshot hash after the rewrite, so the journal it produces stays
internally truthful. Record with the OS screen recorder (macOS:
Cmd-Shift-5); the 30-second pitch is the replay at default pacing.

## Known limitation: epic refs containing `/`

`epic_ref` is routed as a plain path segment (`/epics/<ref>`), validated
against a conservative charset before use. GitHub label names can contain
`/`, and such an epic ref is **unroutable in v1** — the driver still drives
it fine; only the web page 404s on it. There is no slug-encoding scheme yet;
this is a known, accepted gap rather than an oversight.

## Related

- [`kew epic`](../cli/epic.md) — the driver commands and the tick journal
  this page reads.
- [Web UI overview](index.md) — the cockpit shell this page sits alongside.
- [Security: Approval inbox](../security.md#approval-inbox) — the auth model
  this page inherits under `--expose`.
- [Epic DAG design](../design/2026-07-01-epic-dag-design.md) — the original phased design
