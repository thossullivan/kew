---
version: 1
name: kew-console
description: >-
  kew's web console is an instrument panel for a fleet of coding agents. A
  near-black violet ink floor, hairline structure, IBM Plex Mono for every
  label, number and command, IBM Plex Sans for sentences. Two neon accents do
  all the signalling — magenta means "a person is the next move", cyan means
  "live, selected or in review" — and nothing else is allowed to borrow them.
  Dense, quiet, exact: the screen answers "what needs me, and what do I type?"
  before it says anything else.

colors:
  ink: "#0a0714"
  ink-raised: "#0d0a18"
  ink-selected: "#151025"
  ink-track: "#1d1730"
  hair: "#221c33"
  hair-quiet: "#1a1528"
  hair-strong: "#342a4a"
  hair-box: "#2a2240"
  guide: "#16111f"
  text-1: "#ebe6f2"
  text-2: "#b5acc8"
  text-3: "#8d84a3"
  text-4: "#6a6082"
  text-5: "#5a5070"
  accent: "#ff2a6d"
  accent-bright: "#ff5c8f"
  accent-wash: "color-mix(in oklab, #ff2a6d 16%, transparent)"
  accent-2: "#05d9e8"
  accent-2-wash: "color-mix(in oklab, #05d9e8 16%, transparent)"
  halo-magenta: "rgba(255, 42, 109, 0.16)"
  halo-cyan: "rgba(5, 217, 232, 0.16)"
  success: "#2ee6a6"
  warn: "#ffc94a"
  danger: "#ff4365"
  status-blocked: "#b48ade"
  status-cancelled: "#6c5b7b"
  edge-done: "#3a3052"

typography:
  label-11:
    fontFamily: "IBM Plex Mono, ui-monospace, monospace"
    fontSize: 11px
    fontWeight: 400
    letterSpacing: 0.14em
    textTransform: uppercase
  data-13:
    fontFamily: "IBM Plex Sans, system-ui, sans-serif"
    fontSize: 13px
    fontWeight: 400
    lineHeight: 1.45
  data-13-mono:
    fontFamily: "IBM Plex Mono, ui-monospace, monospace"
    fontSize: 13px
    fontWeight: 400
    lineHeight: 1.45
  prose-15:
    fontFamily: "IBM Plex Sans, system-ui, sans-serif"
    fontSize: 15px
    fontWeight: 400
    lineHeight: 1.5
  title-26:
    fontFamily: "IBM Plex Mono, ui-monospace, monospace"
    fontSize: 26px
    fontWeight: 500
    letterSpacing: -0.015em
  readout-44:
    fontFamily: "IBM Plex Mono, ui-monospace, monospace"
    fontSize: 44px
    fontWeight: 500
    lineHeight: 1
    fontVariantNumeric: tabular-nums
  command:
    fontFamily: "IBM Plex Mono, ui-monospace, monospace"
    fontSize: 13px
    fontWeight: 400

rounded:
  card: 4px
  control: 7px
  panel: 11px
  chip: 9999px

spacing:
  unit: 4px
  xxs: 4px
  xs: 8px
  sm: 12px
  md: 16px
  lg: 24px
  gutter: 40px
  section: 32px

components:
  top-nav: "48px bar on {colors.ink}, hairline bottom; wordmark, three tabs (Work, Monitor, Epics), right cluster: working count, needs-you counter, repo, More"
  needs-you-counter: "{colors.accent} diamond + count in {typography.data-13-mono}; a link to /work?filter=needs-you"
  readout-card: "label in {typography.label-11} {colors.text-3}; value in {typography.readout-44}; one-line caption in {typography.prose-15} {colors.text-2}"
  command-block: "{typography.command} on {colors.ink-raised}, 1px {colors.hair-box}, {rounded.control}; Copy button in {colors.accent} with key hint"
  dag-node-card: "{colors.ink-raised} fill, 1px {colors.hair-box}, {rounded.card}, padding 10px 12px; status mark, id in {colors.text-3}, 2-line title, status row in {typography.label-11}, cost"
  dag-node-card-needs-you: "{colors.halo-magenta} fill, 1px {colors.accent} border, magenta diamond; status row in {colors.accent}"
  dag-node-card-selected: "2px {colors.accent-2} ring outside the card"
  popover: "{colors.ink-raised}, 1px {colors.hair-strong}, {rounded.panel}, the only element with a drop shadow"
  chip: "{typography.data-13-mono} in a {rounded.chip} 1px {colors.hair-strong} outline; active = {colors.accent-2} outline + {colors.accent-2-wash} fill"
---

## Overview

kew's console is where one operator watches and steers a fleet of coding
agents. It is read at a glance many times a day and studied closely a few
times. The design serves both readings with one rule: **the screen leads with
what needs a person, says exactly what to do, and keeps everything else
quiet.**

The floor is `{colors.ink}` — a near-black violet, darker than the neon of the
TUI it descends from. Structure is drawn with hairlines (`{colors.hair}`), not
boxes and not shadows. Two neon accents carry all meaning:

- **Magenta** (`{colors.accent}`) means *a person is the next move*: a hold, a
  merge decision, a failed gate. It is rationed. If magenta is on screen, the
  operator has something to do.
- **Cyan** (`{colors.accent-2}`) means *live, selected or under review*: the
  live poll, a selected node, focus, the brush window, an in-review ring.

Everything else — done, queued, idle, history — is drawn in greys and muted
greens so the two accents stay loud by contrast.

**Key characteristics**
- One question per surface: Work = "what is mine to do", Monitor = "what is
  happening", Epics = "where does this drive stand".
- Monospace for anything a person might copy, compare or scan in a column:
  ids, labels, numbers, commands, timestamps. Sans for sentences.
- Five type sizes, no more (11, 13, 15, 26, 44).
- Every state is shown in place. Clicking never navigates away to explain
  itself; links out are explicit and marked ↗.
- Dense like an instrument panel; the density comes from alignment, not from
  small type.

## Colors

### Surfaces
- **Ink** (`{colors.ink}`): the page floor.
- **Ink raised** (`{colors.ink-raised}`): cards, popovers, command blocks.
- **Ink selected** (`{colors.ink-selected}`): the selected row or card fill.
- **Ink track** (`{colors.ink-track}`): progress and brush tracks.
- **Hairlines** (`{colors.hair}`, `{colors.hair-quiet}`, `{colors.hair-strong}`,
  `{colors.hair-box}`): section rules, row rules, control outlines, card
  borders — in that order of weight.

### Text
- `{colors.text-1}` primary values and titles · `{colors.text-2}` sentences ·
  `{colors.text-3}` labels and ids · `{colors.text-4}` secondary metadata ·
  `{colors.text-5}` disabled and placeholders.

### Signal (the only colours allowed to mean something)
| Token | Means | Never use for |
|---|---|---|
| `{colors.accent}` magenta | a person is the next move; primary action (Copy) | decoration, branding, links, charts |
| `{colors.accent-2}` cyan | live, selected, focus, in review, working | errors, emphasis |
| `{colors.success}` green | done, passed, attested | "good" adjectives in prose |
| `{colors.warn}` amber | money (spend, caps) and held | errors |
| `{colors.danger}` red | failed, missing guarantee | needs-you (that is magenta) |
| `{colors.status-blocked}` violet | blocked on a dependency | a kind of run (remediation is a neutral) |

### Run-timeline colours
The epic timeline's bars say what a run was, not what state it is in, so the
kinds that are not a signal take the text ramp's neutrals: dispatch
`{colors.text-2}`, remediation `{colors.text-1}`, any other run
`{colors.text-3}`. Review is `{colors.accent-2}` because a review IS "in
review". Everything else the chart paints is a signal used for its own meaning
— a failed run's `{colors.danger}` outline, the needs-you and held wash
(`{colors.accent-wash}`, magenta left edge), the in-flight wash
(`{colors.accent-2-wash}`), the ready verdict and merge `{colors.success}`, a
not-ready verdict `{colors.warn}` — and each has a legend entry
(`BAR_LEGEND` and `MARK_LEGEND` in `frontend/src/lib/epic/timeline.ts`).

### Status marks
Status is always **shape plus colour**, so it survives colour blindness and
monochrome screenshots. The table lives in `frontend/src/lib/factory/status.ts`
(`STATUS_MARKS`) and is the single source: done = green dot, working = cyan
dot with halo, in review = thick cyan ring, held = thick amber ring, queued =
dashed grey ring, failed = red ×, blocked = hatched violet square, needs you =
magenta diamond with halo, not ready (a review round that ended against the
change) = amber ring with its left half filled. New states get a new shape,
never a reused one.

### Light theme
A light theme exists (`[data-theme='light']`) and maps every token. It is
secondary: design and review in dark, then check light for contrast.

## Typography

### Font family
**IBM Plex Mono** for display, labels, numbers and commands; **IBM Plex Sans**
for sentences and row titles. One superfamily, two voices: mono reads
"console", sans reads "explanation".

### Hierarchy
| Token | Size | Face | Use |
|---|---|---|---|
| `{typography.label-11}` | 11px, uppercase, +0.14em | Mono | section labels, column heads, status words |
| `{typography.data-13}` | 13px | Sans | row titles, list items, popover body |
| `{typography.data-13-mono}` | 13px | Mono | ids, timestamps, costs, chips |
| `{typography.prose-15}` | 15px | Sans | sentences: needs-you summaries, detail-pane text |
| `{typography.title-26}` | 26px / 500 | Mono | page and epic titles |
| `{typography.readout-44}` | 44px / 500, tabular | Mono | the four Monitor readouts |
| `{typography.command}` | 13px | Mono | every runnable command |

### Principles
- Numbers are tabular everywhere (the factory root sets `tabular-nums`).
- Titles clamp to two lines with an ellipsis; never clip mid-glyph.
- A raw reason code (`review_rounds_exhausted`) is never shown as a sentence.
  Show the human label, and the code only as secondary detail.

## Layout

### Spacing
Base unit 4px. Page gutter `{spacing.gutter}` (40px). Section gap
`{spacing.section}` (32px). Row padding 12–16px vertical. Card padding 10×12px.

### Grid
- Fixed top nav; pages fill the viewport height. The Epic page is a column:
  header, graph (grows), standalone strip, bottom rail. The rail is sized by
  its content and capped at 218 px; above the cap it scrolls inside itself
  with the command block first. The canvas takes the rest. No page scroll in
  windows at least 900 px tall; shorter windows may scroll the page.
- Work is master–detail: list left (~400px), detail right. Selection never
  leaves the page.
- Monitor is a readout row, then live runs, then holds (left) beside the event
  stream (right).

### Whitespace philosophy
Tight, aligned, and generous only around the one thing that needs attention.
A needs-you block gets air; done history does not.

## Elevation & Depth

| Level | Treatment | Use |
|---|---|---|
| Floor | `{colors.ink}` | page |
| Ruled | 1px `{colors.hair}` | sections, rows |
| Card | `{colors.ink-raised}` + 1px `{colors.hair-box}` | DAG nodes, command blocks, chips' hover |
| Signal | `{colors.halo-magenta}` / `{colors.halo-cyan}` fill + accent border | needs-you card, selected/live element |
| Float | `{colors.ink-raised}` + `{colors.hair-strong}` + one soft shadow | popovers and menus only |
| Highlight | 1px `{colors.text-1}` outline, offset clear of the target | the demo tour's highlighted element only |

No drop shadows anywhere else. Depth comes from fill contrast and halos.

A demo tour's popover is a Float like any other, with `{rounded.panel}`: its
title in `{typography.prose-15}`, its words in `{typography.data-13}` Plex
Sans, `{typography.command}` for commands, Plex Mono for its progress and
presses. The element it points at gets the
Highlight: a neutral outline over the dimmed page, never magenta (no person is
the next move) or cyan (nothing is live or selected), and no new hue.

## Shapes

| Token | Value | Use |
|---|---|---|
| `{rounded.card}` | 4px | DAG node cards, rows' selection plate |
| `{rounded.control}` | 7px | buttons, inputs, command blocks |
| `{rounded.panel}` | 11px | popovers, the detail block |
| `{rounded.chip}` | pill | filter chips, presets |

## Components

**`needs-you-block`** (Work detail, Epic popover, Monitor caption) — reads top
to bottom: one-sentence summary in `{typography.prose-15}`, then **Why**,
**Since**, **Do this** rows (labels in `{typography.label-11}`), then the
`command-block`. Secondary links (Show in epic →, PR ↗, Issue ↗) in one quiet
row below. Magenta left border on Work.

**`command-block`** — the command exactly as typed, `{typography.command}`,
with a magenta **Copy** button and its key (`c`). Commands are computed by the
backend (`command` on the needs-you row), never parsed out of a reason string.

**`dag-node-card`** — every node is a card, always: status mark, id
(`{colors.text-3}`), two-line title (`{colors.text-1}`), status row in
`{typography.label-11}`, cost right-aligned. Done nodes are quieter (title in
`{colors.text-2}`) but keep their box. The whole card is the click target.

**`dag-node-card-needs-you`** — the one loud card: magenta diamond, magenta
border and halo fill, status row in magenta (`HELD · NEEDS YOU · 18D`).

**`dag-edge`** — smooth orthogonal-ish curves with rounded corners, drawn
behind cards and never across card text. Done edges in `{colors.edge-done}`;
the selected node's lineage in `{colors.accent-2}`; queued edges dashed.

**`dag-canvas`** — pan and zoom (scroll with modifier, drag the floor),
controls bottom-left, minimap bottom-right, **Fit all** (`F`). The first frame
centres on the needs-you node's upstream path; small graphs fit entirely.
Collapse keeps the direct finished parents of every open or needs-you node
drawn and folds the finished history further up into one "N done" group card,
with an edge into the parent it feeds; the card expands on click, and a
selection inside it (a `?node=` link, ← / →) opens it. The toggle is text —
"Done · collapsed (8)" / "Done · shown" — disabled with "No finished chain can
fold here" when nothing folds, and the view refits after it.

**`popover`** — anchored to its node, placed clear of neighbouring cards,
contains the `needs-you-block` (or the node's state for others) and "Open in
Work →". Esc closes it; a second Esc clears the selection.

**`bottom-rail`** (Epic page) — SELECTED · PIPELINE · EVIDENCE · NEEDS YOU in
four columns. Follows the selection; empty state "Select a node to see what it
is waiting on."

**`event-stream`** — range presets (Live 30m, 1h, 6h, 24h, 7d, Custom) as
`chip`s; an overview histogram with a cyan brush (drag to draw, drag body to
pan, handles to resize, it holds after release); a detail strip; filter chips;
rows newest first with `from → to` on status changes and quiet-gap rows; "Load
older" widens the window.

**`hold-row`** (Monitor) — category word in `{typography.label-11}` and its
colour (MERGE magenta, HELD amber, BUDGET amber, REVIEW cyan), `epic / #item —`
sentence, then the command on its own line with Copy.

**`chip`** / **`chip-active`** — pill outline in `{colors.hair-strong}`; active
gets the cyan outline and wash.

**`button-primary`** — magenta fill, `{colors.ink}` text, `{rounded.control}`.
Only for the action that clears a needs-you (usually Copy command).

**`button-quiet`** — transparent, `{colors.text-2}`, hairline outline on
hover. Everything that is not the primary action.

**`link-external`** — text ending in ↗, `target="_blank"`, `rel="noreferrer"`.
Never the default click target of a row or card.

## Do's and Don'ts

### Do
- Lead every surface with what needs a person; put the command one click away.
- Show state in place. Selecting a row or node fills a pane or a popover.
- Draw every DAG node as a card, every time.
- Use shape + colour for status, from `STATUS_MARKS`.
- Keep numbers tabular and right-aligned in columns.
- Write sentences in plain words ("the review round cap was reached"), with
  the reason code as secondary detail.
- Verify every UI change in a real browser (`npm run ui-check`) before review.
- Raise a needs-you only when a person really is next: after the review
  verdict lands, never while a round runs. A run kew is executing is
  "working" (cyan), never a needs-you.
- Count needs-you from one server set everywhere: the header counter, the epic
  legend and the Epics list filter the same set, so their numbers always agree.
- Show "live" (cyan, halo) only with a fresh heartbeat: a tick that started
  within max(3x the interval, the interval + 2x the last tick's duration), one
  window on every surface (`freshBeat`). Otherwise show the signal in `{colors.text-3}` and say "no
  heartbeat".
- Animate only what kew is EXECUTING. One vocabulary says which that is — the
  `motion` field on the run, loop and node DTOs, from `kew/src/kew/motion.py`
  and its copy in `frontend/src/lib/factory/motion.ts` — and `executing` is
  the only one of its four words that moves: a cyan halo on the status mark
  and a running bar whose width grows. The clock ticks once a second, stops
  when nothing executes or the stream drops, and pauses under the pointer; a
  finished node settles to done over ~400ms; `prefers-reduced-motion` switches
  all of it off.
- Animate the present only. A replayed frame draws no halo and no bar, and is
  read against the tick's own instant rather than the wall clock: it keeps its
  mark and its word ("in progress · 20m", as at that tick) and stays still.
- Say what an empty or failed read means, in a sentence ("No events in this
  window · last event 10:46", "tick history could not be read"). Never leave
  a blank box or a bare dash.
- Show only non-zero counts; a zero is noise in a legend or a list.
- Disclose progressively. Every explanation is one line a person acts on, and
  the rest (why, provenance, remedy) is one click away behind `why ▸`. Say a
  reason once, in the place it belongs; a refusal printed in three places is
  read zero times (design review c4: the launchpad's posture refusal).
- Show only what needs a look. A table of resolved values shows its invalid
  and overridden rows; the defaults fold into one "N more from defaults ▸" line.
- Count causes, not instances. One missing credential is one problem with six
  effects, and a finished epic is not a problem at all (the header once read
  "59 down" on a healthy drive).
- Offer a choice from a list wherever the values are known. A model, a lane or
  a runner is picked from what kew knows, never typed and validated afterwards.
- Give every figure on a public page its window, its source and its method,
  beside the figure. Two numbers for one quantity are a defect in the readers,
  never a footnote (design review c9: one evidence pack printed 211 and 220
  merged pull requests, because three readers used three rules).
- Show the product with real frames. A picture of the console on the landing
  page or in the docs is a screenshot of the demo build, taken by a script
  (`www/scripts/shoot-console.mjs`), so it is shot again when the console
  changes. Never draw an imitation of a surface.
- Word an approval hold as what it is: an estimate against a cap ("approve a
  run estimated at $25.00, the per-issue cap"). The person approves the run,
  not a spend up to the estimate.
- Use one token set on the console, the landing page (`www/`) and the docs site
  (`docs/stylesheets/extra.css`). The docs site is dark only, with no toggle.

### Don't
- Don't set a sentence in `{typography.label-11}`. Uppercase tracked mono is for
  labels, column heads and status words; a sentence in it is unreadable past a
  few words. Sentences are `{typography.data-13}` or `{typography.prose-15}`.
- Don't use magenta for anything that is not a person's next move.
- Don't animate anything that is waiting. A queued item never pulses (Dagster's
  does), and an `in_review` pull request is a person's move, not kew's.
- Don't make a row, card or node title a link that leaves the app.
- Don't print a command parsed from a reason string; use the computed one.
- Don't add a sixth type size or a third accent colour.
- Don't add drop shadows outside popovers and menus.
- Don't clip text mid-glyph; clamp with an ellipsis.
- Don't let a component's height depend on a percentage of an indefinite
  parent (the DAG canvas once rendered at 0px tall).
- Don't read a value back from `page.url` after `replaceState`: shallow routing
  moves `page.state` only. Read `page.state` first (the Work, Monitor, Epic
  and Console pages each shipped this bug once).
- Don't render tracker text as HTML (`{@html}`); it is text nodes only.
- Don't let decorative layers (edges, overlays, hint bars) take pointer events
  or sit over card text.
- Don't trap Tab: focus always leaves a widget.
- Don't poll a GitHub read without a cache, a timeout and a failure backoff,
  and pause polls while the tab is hidden (#2264: one tab drained the quota).
- Don't label a thing "verified" unless a check ran on that thing, and name the
  check and where it ran ("3 hashes, 2 links verified in this browser"). A
  thing kew only recorded carries no such word (design review c9: the landing
  page's receipts and its last loop stage each carried it without a check
  behind it).
- Don't move anything by itself on the landing page or in a demo tour. A time
  rail, a set of frames or a tour step changes when the person changes it;
  the console's motion rule (only what kew is executing, only in the present)
  has nothing to animate on a page of recorded frames.
- Don't print a rate beside counts that have different denominators (c9: a
  "merged / opened" column per model read as a merge rate the ledger cannot
  support).

## Responsive Behavior

The console is a desktop instrument first. Design at **1440×900**, verify at
**1280×800**; nothing may overflow horizontally at 1280. Below 1024 the Work
list and detail stack, Monitor's holds move above the stream, its four
readouts fold to 2×2 and take the 26 size, its live runs become two-line rows,
the header's reading cluster folds into More — everything but the needs-you
count — and the DAG canvas keeps pan/zoom rather than reflowing. A size is
taken at a breakpoint, never added: the scale is still the five above.

The header's cluster comes back in two steps, not one. At 1024 it takes the
failure readings (the stale build, the factory health count, the fleet stop
with its resume); the working count and the repo wait until 1280, because
1024px cannot hold those failures and the stop's two-press confirm and both
quiet readings at once, and what it could not hold it scrolled sideways
(#2418). The quiet pair is what gives up its place: nothing is waiting on how
many runs are working, or on which repo.

A fold is arithmetic, not a feeling: a layout with fixed tracks folds at the
width those tracks stop fitting in, and the breakpoint is read off the sum. The
Epic page's selected-node rail is the worked example — 840px of fixed track plus
320px of Pipeline plus the page gutter is 1240px — so it draws the artboard's
four columns from **1280**, folds to 2×2 below that, and stacks into one column
below **1024**, with the canvas keeping a 400px floor under it wherever it has
folded. Folding it at `sm` because a phone showed the bug left the same words
printed over each other at every width from 641 to 1230 (#2333).

The factory header is the same rule over a width that is measured rather than
summed. Its one 56px row cannot shrink its way to fitting — a press squeezed to
a few pixels is a lost press, and a nav label cut to `Wo…` is a way around the
console destroyed — so it folds by carrying fewer things, in the two steps
above, and the width it folds at is the row's own. The row as #2333 found it had
two floors, measured in a browser with every always-on reading on it: **741px**
before the page stopped scrolling sideways, and **929px** before the nav started
ellipsising its three tabs. **1024** is the first breakpoint above both. Folding
it at `sm` instead left the header wider than the window from 641 up: the tabs
ellipsised away and the page still scrolled to reach More (#2419), and `md`
would have bought the scrollbar back and spent the tabs, which is the same bug
with the scrollbar removed. #2418 then took the give off the nav altogether, so
nothing on the phone row shrinks at all and the row is added up from the markup
at every width the design verifies (`factory-header.test.ts`) rather than
measured once.

It must also answer "what needs me, and what do I run?" on a phone. Verify at
**390×844**:
- the page body never scrolls sideways;
- the header keeps the wordmark, the three tabs and the needs-you counter, and
  every other reading folds into More — with a red mark beside More when what
  folded was a failure, so nothing goes quiet behind a closed menu;
- a nav label is never ellipsised, at any width. `Wo… Monit… Epi…` is what the
  row looked like when the nav was the one item allowed to compress (#2418):
  every reading in the cluster has a second way to be had — its `title`, the
  More menu, the page it links to — and a cut nav label has none. The nav and
  its three labels are `shrink-0`, so the phone row fits by carrying less
  rather than by shrinking. Above the fold it carries everything and cannot
  fit that way, so there the give is the cluster's SENTENCES — each a capped
  `truncate` span — and only those: a mark, a count, a press and `More` hold
  their whole width, because a sentence shortened is still readable from its
  `title` and a mark cut in half is a reading destroyed;
- a selected Work item shows full width with a back control;
- every control is at least 32px tall — a chip, pill or select takes the height
  (`TAP_PILL`), a link inside a sentence takes it as vertical padding
  (`TAP_INLINE`), which an inline box is tapped by without entering the line it
  sits in, and both hand the artboard's own height back from `sm` up;
- the Work pipeline keeps its stage order and its dot vocabulary and scrolls
  inside its own box: a stage column narrower than its own 11px header printed
  "IMPLEMENTPUBLISHREVIEW" with the captions on each other, so the lane carries
  a floor (`laneMinWidth`) and the page still never scrolls sideways;
- the Epic page's driver tick strip is fitted to the box it sits in rather than
  drawn at its 760px design width. That box is clipped at the viewport and the
  strip puts its NEWEST attempt last, so what a 760px strip lost on a phone was
  the recent ticks — the half a reader checks to see whether the driver is still
  moving — while the oldest ones kept their pixels, and no sideways scroll
  reached them (#2473, `fitStripWidth`). The fitted strip then shows FEWER
  attempts, and that is the trade a narrow window makes: it draws the newest
  ones its own width can keep apart (`stripLimit`) and labels itself with that
  count — 76 in a 310px box against 189 at the design's 760px. Not "the same
  attempts merged tighter": the batcher's 4px floor is a cliff and not a
  squeeze, since a merge widens its batch and so pulls in the mark after it —
  at 310px, 77 evenly paced attempts keep 77 bars and 78 collapse into one bar
  labelled "189 attempts". Less history is a reading; one bar is not;
- a spend gauge prints the two ends of its axis only, the floor and the cap:
  three money labels do not fit across a folded readout, and the halfway one
  printed over the cap's;
- on the Epic page the popover becomes a bottom sheet, and the key hints and
  the minimap are hidden below 1024;
- the Epic page's selected-node rail is one column of full-width sections, the
  bottom tier of the fold above, and the page's own scroller takes the height
  the canvas's 400px floor will not give up.

A short window is its own fold, by height and not by width: under **700px**
the Epic page's rail collapses to the one line naming the selected node, with
a disclosure that opens the four sections again. A fixed band at the foot of a
600px-tall window is a third of the canvas, and a 1440×600 desktop has that
problem as squarely as a phone does.

## Iteration Guide

1. Change one component at a time and name it by its key above.
2. Use tokens (`var(--accent)`, Tailwind `text-text-2`), never raw hex.
3. Check the artboards in `frontend/design/factory-ui/` (`UX*.html` are the
   current targets) and run `npm run shoot -- <route> <artboard>`.
4. When in doubt about emphasis: more space and a heavier text colour before
   any accent.
5. A new surface's browser checks go in a new module,
   `frontend/scripts/ui-checks/<name>.mjs`, exporting
   `async function checks({ browser, base, context, check, visit, expect, sleep, sizes })`.
   Never edit the runner, `scripts/ui-check.mjs`: it imports every module in
   that folder in name order, so two UI changes that each add a module do not
   conflict (#2570).
5. If something needs a person, it is magenta. If it does not, it is not.

## Known Gaps

- The older token generation (`--bg`, `--surface`, `--fg`, `--accent-dim`) is
  still used by pre-factory pages; new work uses the factory set above.
- The stall banner on the Epic page still prints journaled reason text, which
  can contain stale commands.
