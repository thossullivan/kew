# kew frontend

The SvelteKit single-page app `kew serve` hosts. Routes live in `src/routes`,
the design-system pieces in `src/lib`.

```
npm install
npm run dev            # /api proxied to a live `kew serve` on :8765
npm run dev:fixtures   # /api answered from fixtures/factory (no kew serve)
npm test               # vitest
npm run check          # svelte-check
npm run build
npm run build:demo     # the static demo console in build-demo/ (no backend)
npm run demo-check     # the demo's browser checks against build-demo/, or `-- <base-url>`
```

`npm run demo-check -- [<base-url>]` (`scripts/demo-check.mjs`) runs the
`scripts/ui-checks/demo-*.mjs` modules against a demo: the local `build-demo/`
(run `npm run build:demo` first) when no URL is given, or a deployed one such
as `https://demo.kew.dev`, where it skips, by name, each check that reads the
local build. One check is about the host and is never skipped: a file under
`/_app/immutable/` that no build writes must answer 404 with no `immutable` in
its Cache-Control, and each script the page loads must answer 200. It also
builds the docs (`mkdocs build --strict`, via `uvx`),
clicks every "See it in the demo" link into the demo and checks the tour opens
on that step, and writes every tour step at 1440x960, 1366x650 and 390x844 to
`.shots/demo/<stamp>/` with a `manifest.json` for review. It needs a Chromium,
found as `npm run ui-check` finds one.

## `npm run build:demo` — the console with no backend

`npm run build:demo` (`demo/vite.demo.config.ts`) writes a static,
backend-free console to `build-demo/`, servable from the root of any host that
answers as the site block in `www/DEPLOY.md` ("Caddy site block") does: a
page path that names no file falls back to `/index.html`, and a missing file
under `/_app/` answers 404, never the fallback. It is the same app: the
config defines `__KEW_DEMO__` as `true`, and `src/hooks.client.ts` then
installs `src/lib/demo/` before the first `load` runs. Every `/api` read is
answered in the browser out of the fixture set bundled at build time (one
setting, `demo/fixture-set.ts`) through the same helpers the dev fixture server
uses (`fixtures/core.ts`), the live streams open and stay quiet, every write
answers 405 with a sentence saying what it would have done and the command that
does it (`src/lib/demo/writes.ts`), and a banner says the data is synthetic.

`vite.config.ts` defines the flag as `false`, so `npm run build` drops every
demo module — as long as nothing but `src/hooks.client.ts` imports one, which
`src/lib/demo/boundary.test.ts` holds every shipped file under `src/` to. The
default `npm run ui-check` then greps `build/` for the demo's sentinel and its
own words (the banner, "Demo: this would"), driver.js and fixture data.
The shell's three chrome slots (`src/lib/shell/chrome-slots.svelte.ts`) are
where demo chrome mounts — a banner, an entry in the header's More menu, a page
foot — and draw nothing in the normal build.

### Guided tours

The demo's tours run on driver.js (pinned to `1.9.0`, bundled; only demo code
may import it), from the Tours entry at the top of the More menu, at 1024px and
wider. Below that the entry gives way to one line, and a tour open when the
window narrows ends. A tour is one file, `demo/tours/<id>.json`:

```json
{
  "id": "needs-you",
  "title": "What needs me, and what do I run?",
  "start": "/work",
  "steps": [
    { "route": "/work", "target": "needs-you-count", "docs": "web/index.md",
      "anchor": "cockpit-shell", "passage": "needs-you/1" }
  ]
}
```

`target` is the value of a `data-tour="..."` attribute on the console, never a
CSS class or text match. A step may name a `query`, key by key, that it opens
its route with: `"query": { "mode": "replay", "t": "3900" }` puts the epic page's
replay on one moment, so the step shows the same graph every time (never
`tour` or `step`, which are the tour's own link, and never `"mode": "replay"`
without a `t`). Every step owns the epic page's `mode`, `t`, `speed`, `view`
and `select`: it sets the ones its query names and takes the rest out, so a
replay step draws the graph with nothing selected and a step with no query
opens the page live; every other key stays as the visitor has it. The `follow-an-epic` tour
steps through the demo epic this way, and `demo-follow-an-epic.mjs` checks each
step's graph against the journal frame its `t` names. A step about one node
names it, `"card": "107"`, highlights that node's card inside its target
(`.svelte-flow__node[data-id="107"]`) rather than the whole graph, and selects
it with `"node": "107"` in its query, so the page frames that card at 100%
(its text at 11px or more) and the rail reads the same node; the node popover
the link opens is shut with its own ×. `"fit": true` presses the graph's own
Fit all, for a step about the whole graph only: the build refuses it on a card
step. Wherever driver.js puts a popover, the controller moves it off its
target, off the page dialog the target sits in (the node popover), and, where
the window has room, off the region a step names as `"clear"` (the graph a
sentence compares across); a target at the window's edge has its outline drawn
inside it. A step's words are not in the file: they are a
passage in its `docs` page, numbered from 1, which mkdocs shows as normal text:

```markdown
<!-- demo-step: needs-you/1 -->
**What needs you**

The header counts the waits that are yours.
<!-- /demo-step -->
```

`demo/tour-bundle.ts` reads both at build time. `npm run build:demo` fails on
a step with no passage, a passage marked twice or read by no step, a passage on
another page than its step links to, an anchor its page does not have, and a
passage over 40 words, title and body together.
`?tour=<id>&step=<n>` deep-links a step; Escape ends a tour. A link no tour can
open (a tour the build does not have, a step past its end, a step that is not
a whole number from 1 such as `step=0` or `step=abc`, or a window under
1024px) opens nothing and is taken out of the address bar. The browser check
`demo-tours.mjs` opens every step's route, with its query, and fails when its target is gone.

No tour ends on the Build history foot: while the build bundles the
`*.sample.json` provenance, the foot's figures are invented and say "sample
data", so each tour ends on its own point instead.

The tours include `needs-you` (`demo/tours/needs-you.json`), "What needs me,
and what do I run?": the header's needs-you count, the queue on Work, the
epic's heartbeat, then a merge decision and a magenta hold with its Copy
command on Monitor. `demo-tour-needs-you.mjs` runs it start
to end at 1440x960, 1366x650 and 1024x768, checks each popover against its
passage, read from the markdown, and fails a step whose target is an empty box
or is cut off by the window or by the panel that scrolls it. Before a step
shows, the tour scrolls its target to the middle of that panel
(`src/lib/demo/tours/reveal.ts`), since driver.js scrolls only the window.

`refused-pr` (`demo/tours/refused-pr.json`), "Why was this PR refused?", walks
the world's refused item: the review's `NEEDS_CHANGES` send-back and the
`READY` after the fix on their run pages, then the failed merge verify and
its held-out test ids on Work. Its Work steps name
`"query": { "item": "107" }`; `item` is not a key a step owns.
`demo-tour-refused-pr.mjs` runs it at 1440x960, 1366x650 and 1024x768 and
checks each step's reading on the screen against `demo/world/manifest.json`.

`where-did-the-money-go` (`demo/tours/where-did-the-money-go.json`), "Where did
the money go?", follows spend upward: one run's cost on its run page, its
node's runs and total on the epic graph (`?node=`), today's and this week's
spend, each against its cap, on Budget, and last the epic's drain on the
Epics list, the sum its node cards add up to. Every step says its figure is list price. `demo-tour-money.mjs` runs it
at 1440x960, 1366x650 and 1024x768 and reads the figures off the page: the
node's popover links the run at its run page's cost, its runs sum to its
lifetime line and its graph card's drain figure (one figure here because every
world node's lifetime is its drain), and the cards sum to the epic's drain. At
the short laptops it also checks Back from step 3 and a deep link to step 2,
each reopening the node's popover.

### The demo world

The set the demo bundles is `demo/world/`, a synthetic factory, never
`fixtures/factory/` (which holds real epic refs, run ids and names). One
invented repository, `acme/ledger`, holds at once an epic in flight
(`ledger-v2`, with a blocked node, a node waiting on a merge decision and a
failed node to retry, and its tick history for replay), four needs-you items,
a PR refused twice (a review send-back, then a merge verify that failed on
held-out tests and holds the merge for a person's decision), and spend that
reconciles run → node → epic → budget.
`demo/world/manifest.json` names the refs the demo checks and tours read.

Its content is invented, and so are the timings every published demo ships:
`www/deploy-demo.sh` refuses to stage a world whose `timings.json` is not
`"source": "fake"`, so its durations, costs and token counts are drawn from a
seeded fake corpus, not from kew's own runs. A world built from real timings
needs the owner's approval first. `kew/scripts/demo_world_timings.py`
aggregates the run-report corpus (`[run_reports] sink_path`, by default
`~/.local/share/kew/runs/`) into `demo/world/timings.json`: per run kind, the
median and p90 of duration, cost, fresh input, output and cache-read tokens,
and the success rate — nothing per run. A kind with fewer than 20 distinct
runs publishes nothing; figures are coarsened (costs to $0.10, durations to
whole minutes, tokens to two significant figures but never finer than tens,
success rates to steps of 0.05); and the file is held to an allowlist of keys,
types and run kinds before it is written. It declares its `source`: `fake` or
`corpus`. `demo/world/generate.mjs` builds the world from it with a fixed
seed, and a kind the timings do not publish draws a fixed invented spread.

The committed `timings.json` is built from a seeded fake corpus
(`"source": "fake"`, which a test pins to that corpus's aggregate). From `kew/`:

```sh
uv run python scripts/demo_world_fake_corpus.py "$TMPDIR/fake-runs"
uv run python scripts/demo_world_timings.py --corpus "$TMPDIR/fake-runs" --source fake
```

At release the operator regenerates it from the real corpus on the operator
host (`"source": "corpus"`; the tests then hold it to the allowlist, the floor
and the rounding, and no test is edited), then the world, then scans it:

```sh
(cd ../kew && uv run python scripts/demo_world_timings.py)   # reads the configured sink
node demo/world/generate.mjs        # byte-identical on a rerun
node demo/leak-scan.mjs demo        # source rules; --bundle for build-demo/
```

`src/lib/demo/world.test.ts` types every world file through `api.ts` and
holds the story above; `src/lib/demo/routes.test.ts` holds every `/api` read
the console's source names to a world answer (or a listed, checked reason
there is none); `scripts/demo-world.test.ts` holds the committed world to the
generator's output and every leak-scan rule to a planted example.
`ui-checks/demo-world.mjs` reads it on the built demo, and scans the bundle.
The leak scan's denylist is the names `fixtures/factory/` holds unioned with
the committed `demo/leak-scan.deny.json`, so editing the fixtures alone can
never shrink it.

### How each surface was built

The demo's page foot says how the surface above it was built: the runs,
PRs and list cost of each feature in `demo/features.toml` that names its
route, collapsed to one line and expanded to a small ledger
(`src/lib/demo/BuildHistory.svelte`). The figures are real build history:
`kew report provenance` writes `demo/provenance/provenance.json` and
`runs.json` at release, never committed, and until they exist the committed
`*.sample.json` copies stand in. `demo/provenance-bundle.ts` checks the pair
against an allowlist schema of keys, value types and kew's own vocabulary
(`demo/provenance-schema.ts`; an extra key, a wrong type or free text fails
the build). Every PR link must be on one repository: kew itself
(`demo/public-repo.json`) for the real pair, the invented `acme/kew` for the
samples. Every feature's routes, epics and issues must be the ones its
features file names: `demo/features.toml` for the real pair,
`demo/provenance/features.sample.toml` for the samples. The foot draws every
PR as a plain number, never a link: the real PRs are in kew's private
development repository, and the samples' are on an invented owner's
repository, someone else's URL space. A sample build ends the foot's line
with "sample data" (the plugin says which file shipped, beside the file). Model ids come from
kew's catalog or are `other`. Error categories come from
`demo/error-categories.json` or are `other`: the export publishes a code only
when it is in `PUBLISHED_ERROR_CATEGORIES` (`kew/src/kew/report_provenance.py`),
which `kew/tests/test_error_category_vocabulary.py` holds equal to that file.
The export also drops a run whose id is not a uuid and writes an empty item
id as `null`. The checked
`provenance.json` ships as `build-demo/demo-provenance.json`, read from the
site itself, never through the `/api` shim, with the routes it names bundled
beside it so a surface no feature names draws no foot at all. A publish
needs the real export and the owner's approval (`www/deploy-demo.sh`); a build
made without the export bundles the committed samples.

The leak scan exempts no file whole: `exempt` in `demo/leak-scan.allow.json`
names, per exact path and mode, the JSON key paths (or TOML keys) whose
values skip the owner rule or the denylist, and every other value and rule
still holds there (`demo/leak-scan.mjs`). `scripts/demo-provenance.test.ts`
holds the schema and the samples to it; `ui-checks/demo-provenance.mjs`
reads the foot on the built demo.

## `npm run ui-check` — the browser checks

Four UI changes passed vitest and an adversarial review and were still broken
on screen: the epic canvas rendered at 0 height, the Monitor presets ignored
the URL, graph edges took the clicks meant for nodes, and a Work row click did
not open the item. Layout, a real wheel event, hit testing and client routing
are not reachable from jsdom, so vitest cannot see any of them.

`npm run ui-check` (`scripts/ui-check.mjs`) drives a real Chromium over the
fixture-mode dev server and asserts them. It starts `vite dev` with
`KEW_UI_FIXTURES=1` on a free port itself and always stops it again, including
on a failure, so nothing has to be running first. It prints one line per check
and exits non-zero if any fails:

- every top-level route (`/work`, `/monitor`, `/epics`, `/epics/launch-fixes`)
  loads with no page errors and no console errors;
- the epic canvas `.dag` has a non-zero size and at least one node;
- a node click opens the popover and writes `?node=`;
- the wheel over the canvas changes the viewport scale;
- a Work row click opens that item in the detail pane;
- a Monitor preset click changes the visible range.

…and the rest, one module per surface. `scripts/ui-check.mjs` is only the
runner — the browser, the dev server, `check()`, `visit()`, `expect()` and the
summary. Each surface's checks live in `scripts/ui-checks/<name>.mjs`, exporting
`async function checks({ browser, base, context, check, visit, expect, sleep, sizes })`,
and the runner imports every module in that folder in name order. A new
surface adds a new module and never edits the runner, so two UI changes no
longer conflict over one file (#2570). What more than one module reads (the
fixture epic, `SETTLE_MS`, `widestBox`) is in `scripts/ui-check-lib.mjs`;
`scripts/ui-check.test.ts` holds the split to the 79 checks it started from.

A module named `demo-*.mjs` runs only against the static demo build, behind a
standard-library server (`scripts/static-server.mjs`) that does the SPA
fallback, answers 404 for a missing file under `/_app/` and sends
`www/Caddyfile`'s Content-Security-Policy; every other
module runs only against the dev server. The default run builds first, before
any server or browser starts: `npm run build`, whose output must hold no demo
sentinel, no driver.js class and no fixture run id, then `npm run build:demo`.
A scoped run builds the demo only when it names a `demo-*` module.

`npm run ui-check -- work-rows epic-rail` runs only the named modules (with or
without `.mjs`), so a sandboxed agent checks the surfaces it touched without the
whole suite. A name with no module fails before any browser starts, and a
scoped run with no Chromium, or one whose modules make no check, fails rather
than skipping: it was asked about those surfaces, and a skip would pass them
unseen. The merge verify always runs every module (#2984).

**No browser is downloaded.** `playwright-core` is a dev dependency and is the
driver only; the Chromium is one the machine already has, looked for in this
order:

1. `$KEW_UI_CHECK_CHROMIUM` — and if it is set, it must exist;
2. the Playwright cache — `~/Library/Caches/ms-playwright` on macOS,
   `~/.cache/ms-playwright` on Linux. To put one there:
   `npx playwright install chromium-headless-shell`.

With neither, an unscoped run prints `skipped: no Chromium` and exits 0 — a machine without
a browser must not go red over a check it cannot make. The merge verify runs it
on a machine that has one, for UI changes.

To see it bite, break something on purpose — `height: 0` on `.dag` in
`src/lib/epic/DagView.svelte` reports `.dag rendered at 1440x0`.
