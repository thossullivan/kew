#!/usr/bin/env node
/**
 * The design-review gallery: photograph every console surface the same way,
 * every cycle, so two cycles can be compared shot for shot.
 *
 *   npm run dev:fixtures                         # /api from fixtures/factory
 *   node scripts/gallery.mjs http://127.0.0.1:5173 [out-dir]
 *   node scripts/gallery.mjs http://127.0.0.1:8000 --live   # a kew serve
 *
 * A base that does not serve the fixture set is photographed in live mode
 * even without `--live`: the shots that need fixture data are skipped and
 * listed under the manifest's `skipped`, and no override is applied. In their
 * place it photographs up to two of the live roster's active epics
 * (`pickLiveEpics`) and records which, and why, under the manifest's `epics`.
 *
 * Writes one PNG per shot and width into `frontend/.shots/gallery/<stamp>/`
 * (gitignored), plus `manifest.json`: each shot's name, route, width, file,
 * and the page errors and console errors it raised. A shot with errors is
 * still written: the review reads the manifest, not only the pictures.
 *
 * Needs `playwright-core` (a dev dependency, #2273) and a Chromium. The
 * browser is never downloaded: `$KEW_UI_CHECK_CHROMIUM`, else the newest
 * headless shell in the Playwright cache. A design review of the console
 * runs this, then reads the manifest and the shots it writes.
 */
import { existsSync, mkdirSync, readFileSync, readdirSync, writeFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { join } from 'node:path';

/** Desktop and phone. DESIGN.md: no horizontal page scroll at either. */
const WIDTHS = [
  { label: 'desk', width: 1440, height: 900 },
  { label: 'phone', width: 390, height: 844 }
];

/**
 * The surfaces, in the order a reviewer reads them. Routes name fixture data
 * (`frontend/fixtures/factory/`): `launch-fixes` and its node 2055, the Linear
 * epic `go-live-2026-09`, Work items 2055, 2060 and SFD-229, run `f5dcb6a5`.
 * Plan a dispatch mounts only where dispatch is the next move, so its shots
 * open #2060, the fixture ready queue's first item (#2785). `then`
 * is a list of steps run after the load settles: `{ click: selector }`,
 * `{ hover: selector }`, `{ key: 'Tab' }`, `{ wheel: dy }`. A step that cannot
 * run is recorded in the manifest, never fatal: a gallery of a half-built cycle
 * is still a gallery. `shows` names what the steps must leave on screen;
 * `scripts/ui-checks/gallery-shots.mjs` reads it, so a shot that stops
 * reaching its surface goes red.
 *
 * A surface whose affordance only appears over certain data needs the shot
 * that reaches it. The needs-you block's `Do this` row and its Copy button are
 * drawn only where the wait HAS a command, and the committed merge decision on
 * #2055 has none — the merge is made on the host, so `kew.waits` computes no
 * command for it. `work-needs-you` is the shot that photographs that half:
 * SFD-229, the go-live round-cap hold, whose command `kew.waits` really does
 * compute (`frontend/fixtures/README.md`).
 *
 * `fixtures` is the other half of that idea, for a surface whose shape is a
 * property of the WHOLE payload rather than of one row in it: `{ '<api path>':
 * '<file under frontend/fixtures/>' }` answers those reads from the named file
 * for this shot alone, leaving `npm run dev:fixtures` serving the one set it
 * always has. `work-sole-wait` is why it exists — the Work tab expands the one
 * needs-you block only when exactly ONE thing needs a person, and the
 * committed set holds two because the world it draws really has two
 * (`frontend/fixtures/README.md`).
 *
 * `fixtureData: true` marks a shot whose route names a fixture id. Against a
 * live `kew serve` those routes 404, and an override would photograph fixture
 * data inside a live page, so live mode skips both kinds (`planShots`).
 */
export const SHOTS = [
  { name: 'work', route: '/work' },
  {
    name: 'work-sole-wait',
    route: '/work',
    fixtures: { '/api/dashboard': 'scenarios/dashboard-sole-wait.json' }
  },
  // #2515 — the first-run card: no work, no run history, and one readiness
  // check failing, so the shot shows the check, its own fix command (with the
  // magenta Copy) and the next command.
  {
    name: 'work-first-run',
    route: '/work',
    fixtures: {
      '/api/dashboard': 'scenarios/dashboard-first-run.json',
      '/api/dashboard/work': 'scenarios/dashboard-work-first-run.json',
      '/api/runs': 'scenarios/runs-first-run.json',
      '/api/readiness': 'scenarios/readiness-first-run.json'
    }
  },
  { name: 'work-item', route: '/work?item=2055', fixtureData: true },
  { name: 'work-needs-you', route: '/work?item=SFD-229', fixtureData: true },
  { name: 'monitor', route: '/monitor' },
  { name: 'epics', route: '/epics' },
  { name: 'epic-graph', route: '/epics/launch-fixes', fixtureData: true },
  { name: 'epic-node', route: '/epics/launch-fixes?node=2055', fixtureData: true },
  {
    name: 'epic-zoomed',
    route: '/epics/launch-fixes',
    fixtureData: true,
    then: [{ hover: '.dag' }, { wheel: -300 }]
  },
  { name: 'epic-timeline', route: '/epics/launch-fixes?view=timeline', fixtureData: true },
  { name: 'epic-linear', route: '/epics/go-live-2026-09', fixtureData: true },
  // #2298 AC3: this shot's health payload has ONE missing routed image and one
  // missing UNROUTED image, so it shows the red × and the header's "1 down"
  // beside a grey row that counts nowhere. The committed set's routed images
  // are present (#3461), so no other screen carries that "1 down".
  {
    name: 'health',
    route: '/health',
    fixtures: { '/api/health': 'scenarios/health-image-missing.json' }
  },
  // #3019 — the prompt catalog with the pinned reviewer selected, so the shot
  // shows its version chip, the "edit through a PR" line and the first open
  // section of its preview.
  { name: 'prompts', route: '/prompts?role=adversarial_review' },
  // #2300 — the run page's split, and the same page as a Gantt bar opens it:
  // one attempt named and one instant focused, so the marker and the
  // highlighted row are in the photograph.
  { name: 'run-events', route: '/runs/f5dcb6a5', fixtureData: true },
  {
    name: 'run-events-focused',
    route: '/runs/f5dcb6a5?attempt=1&focusedTime=2026-09-24T15%3A44%3A20%2B00%3A00',
    fixtureData: true
  },
  // #2790 — the c4 fixes, each photographed open: the Cmd-K palette, the
  // launchpad's Details drawer, the /config model listbox, and a selected epic
  // node after a toolbar Zoom-in click (the selection survives the click).
  {
    name: 'palette-open',
    route: '/work',
    then: [{ key: 'Control+k' }],
    shows: '[role="dialog"][aria-label="Go to"]'
  },
  {
    name: 'launchpad-drawer',
    route: '/work?item=2060',
    fixtureData: true,
    then: [{ click: 'section[aria-label="Plan a dispatch"] button:has-text("Details")' }],
    shows: '[role="dialog"][aria-label^="Plan a dispatch"]'
  },
  {
    name: 'config-model-picker',
    route: '/config',
    then: [{ click: 'li:has-text("Agent model") button:has-text("Override")' }],
    shows: '[role="dialog"][aria-label="Override Agent model"] [role="listbox"]'
  },
  {
    name: 'epic-node-zoom-in',
    route: '/epics/launch-fixes?node=2055',
    fixtureData: true,
    then: [{ click: 'button[aria-label="Zoom in"]' }],
    shows: '.dag .svelte-flow__node.selected'
  }
];

/** True when `--live` is among the gallery's arguments. */
export function liveFlag(argv) {
  return argv.includes('--live');
}

/**
 * The shots one run takes, and the ones it leaves out with the reason. In live
 * mode a shot is skipped when its route names fixture data or it carries
 * overrides, so no picture mixes the two; the fixture run takes every shot.
 *
 * Live mode also photographs the `epics` `liveEpics` chose (`liveEpicShots`).
 * The linear shot is the plain epic page, which draws the graph again, so it is
 * taken only of an epic `isSingleChain` read as linear (#2899); any other epic's
 * linear shot is skipped with the reason, like a fixture-only shot.
 */
export function planShots(shots, { live, epics = [] }) {
  if (!live) return { shots, skipped: [] };
  const taken = [];
  const skipped = [];
  for (const shot of shots) {
    const reason = shot.fixtures
      ? 'answers reads from fixture overrides'
      : shot.fixtureData
        ? 'route names fixture data'
        : null;
    if (reason === null) taken.push(shot);
    else skipped.push({ name: shot.name, route: shot.route, reason });
  }
  for (const epic of epics) {
    for (const shot of liveEpicShots([epic])) {
      const reason = shot.view !== 'linear' || epic.linear === true ? null : notLinear(epic);
      if (reason === null) taken.push(shot);
      else skipped.push({ name: shot.name, route: shot.route, reason });
    }
  }
  return { shots: taken, skipped };
}

/** Why a chosen epic's linear shot is not taken. */
function notLinear(epic) {
  return epic.linear === false
    ? 'the epic is not a single chain, so its plain page is the graph shot again'
    : `the epic graph was not read (${epic.graphError ?? 'no answer'}), so it is not known to be a single chain`;
}

/**
 * True when an epic snapshot's `nodes` and `edges` (`[from, to]` pairs) make one
 * chain: every node has at most one edge in and one out, and walking from the
 * only node with none in reaches every node. A fork, a join, two separate
 * chains, a cycle, an empty graph or an unread one is not linear.
 */
export function isSingleChain(snapshot) {
  const ids = (snapshot?.nodes ?? []).map((n) => n?.id);
  const edges = snapshot?.edges ?? [];
  if (ids.length === 0 || new Set(ids).size !== ids.length) return false;
  const next = new Map();
  const into = new Map();
  for (const [from, to] of edges) {
    if (!ids.includes(from) || !ids.includes(to)) return false;
    if (next.has(from) || into.has(to)) return false;
    next.set(from, to);
    into.set(to, from);
  }
  const heads = ids.filter((id) => !into.has(id));
  if (heads.length !== 1) return false;
  let seen = 1;
  for (let at = heads[0]; next.has(at); at = next.get(at)) seen += 1;
  return seen === ids.length;
}

/** The most live-roster epics one live gallery photographs. */
export const LIVE_EPIC_CAP = 2;

/**
 * Which of the live roster's epics a live gallery photographs, and why (#2797).
 *
 * The fixture epic shots are skipped against a live serve, and a live gallery
 * with no epic page lost the only evidence for what fixture epics never show
 * (c4's N2 phone strip overflow, N6 stall banner). So live mode takes up to
 * `LIVE_EPIC_CAP` ACTIVE epics — not archived, not `complete`, the line
 * `$lib/epic/roster` draws — in roster order: first one that is stalled or has
 * needs-you, then one that is progressing. `needsYou` is the dashboard's
 * `needs_you_by_epic`; an absent epic is 0. An empty pick says why in `note`.
 */
export function pickLiveEpics(epics, needsYou = {}) {
  const active = (epics ?? []).filter(
    (e) => e && typeof e.epic_ref === 'string' && e.archived !== true && e.signal !== 'complete'
  );
  const attention = (e) => {
    const why = [];
    if (e.signal === 'stalled') why.push('stalled');
    const n = needsYou[e.epic_ref] ?? 0;
    if (n > 0) why.push(`needs-you (${n})`);
    return why.join(', ');
  };
  const chosen = [];
  const first = active.find((e) => attention(e) !== '');
  if (first) chosen.push({ ref: first.epic_ref, why: attention(first) });
  const moving = active.find((e) => e.signal === 'progressing' && e !== first);
  if (moving) chosen.push({ ref: moving.epic_ref, why: 'progressing' });
  const picked = chosen.slice(0, LIVE_EPIC_CAP);
  const note = picked.length
    ? null
    : active.length
      ? `no active epic is stalled, has needs-you or is progressing (${active.length} active)`
      : 'the live roster has no active epic';
  return { chosen: picked, note };
}

/**
 * The shots for the epics `pickLiveEpics` chose: graph, timeline and linear,
 * each at every width like any other shot. kew has no third canvas view, so
 * `linear` is the plain epic page — the route shape the fixture `epic-linear`
 * shot takes. The ref is opaque: encoded into the route, made file-safe in the
 * name.
 */
export function liveEpicShots(chosen) {
  return chosen.flatMap(({ ref }) => {
    const route = `/epics/${encodeURIComponent(ref)}`;
    const slug = ref.replace(/[^A-Za-z0-9._-]/g, '_');
    return [
      { name: `live-epic-graph-${slug}`, route: `${route}?view=graph`, epic: ref, view: 'graph' },
      { name: `live-epic-timeline-${slug}`, route: `${route}?view=timeline`, epic: ref, view: 'timeline' },
      { name: `live-epic-linear-${slug}`, route, epic: ref, view: 'linear' }
    ];
  });
}

/** Run one `then` step; a throw is the caller's to record. */
export async function step(page, s) {
  if (s.click) await page.click(s.click, { timeout: 3000 });
  else if (s.hover) await page.hover(s.hover, { timeout: 3000 });
  else if (s.key) await page.keyboard.press(s.key);
  else if (s.wheel) await page.mouse.wheel(0, s.wheel);
  await page.waitForTimeout(400);
}

const SETTLE_MS = 2500;

/** Where a shot's `fixtures` paths are resolved against. */
const FIXTURES = new URL('../fixtures/', import.meta.url);

function die(message) {
  console.error(`gallery: ${message}`);
  process.exit(1);
}

/**
 * Answer one shot's `fixtures` reads from the committed files it names, before
 * anything is loaded.
 *
 * The bodies are read from disk here — not written into this script — so the
 * payload a reviewer sees in the shot is a committed fixture that
 * `src/lib/fixtures.test.ts` types and holds, exactly like the set the dev
 * server serves. The match is on the pathname alone, so the app's own
 * `?after=`/`?limit=` queries are covered.
 */
async function overlay(page, fixtures) {
  for (const [path, file] of Object.entries(fixtures)) {
    const body = readFileSync(fileURLToPath(new URL(file, FIXTURES)), 'utf8');
    await page.route(
      (url) => url.pathname === path,
      (route) =>
        route.fulfill({
          status: 200,
          contentType: 'application/json',
          headers: { 'Cache-Control': 'no-store' },
          body
        })
    );
  }
}

function chromium() {
  const named = process.env.KEW_UI_CHECK_CHROMIUM;
  if (named) return existsSync(named) ? named : die(`$KEW_UI_CHECK_CHROMIUM=${named} does not exist`);
  const caches = [
    join(homedir(), 'Library/Caches/ms-playwright'),
    join(homedir(), '.cache/ms-playwright')
  ];
  for (const cache of caches) {
    if (!existsSync(cache)) continue;
    const shells = readdirSync(cache)
      .filter((d) => d.startsWith('chromium_headless_shell-'))
      .sort()
      .reverse();
    for (const shell of shells) {
      for (const sub of ['chrome-headless-shell-mac-arm64', 'chrome-headless-shell-mac-x64', 'chrome-headless-shell-linux64']) {
        const bin = join(cache, shell, sub, 'chrome-headless-shell');
        if (existsSync(bin)) return bin;
      }
    }
  }
  return die('no Chromium found: set $KEW_UI_CHECK_CHROMIUM or populate the Playwright cache');
}

async function playwright() {
  try {
    return await import('playwright-core');
  } catch {
    return die('playwright-core is not installed (npm install, after #2273 adds it as a dev dependency)');
  }
}

/**
 * Whether `base` is the fixture dev server. Only `npm run dev:fixtures`
 * answers the fixture run `f5dcb6a5` with that run: a live `kew serve` 404s it
 * (or refuses the read without a session), so anything else is live.
 */
async function servesFixtures(base) {
  try {
    const res = await fetch(`${base}/api/runs/f5dcb6a5`, { signal: AbortSignal.timeout(5000) });
    return res.ok && (await res.json())?.run_id === 'f5dcb6a5';
  } catch {
    return false;
  }
}

/** A live serve's JSON at `path`, or `null` with the reason it could not be read. */
async function readJson(base, path) {
  try {
    const res = await fetch(`${base}${path}`, { signal: AbortSignal.timeout(5000) });
    if (!res.ok) return { body: null, error: `${path} answered ${res.status}` };
    return { body: await res.json(), error: null };
  } catch (e) {
    return { body: null, error: `${path} unreadable (${e.cause?.code ?? e.message})` };
  }
}

/**
 * Read the live roster and choose its epics. A roster that cannot be read is a
 * manifest note, never fatal; an unreadable needs-you map only narrows the pick
 * to stalled and progressing epics, and the note says so. Each chosen epic's
 * graph is read for `linear` (`isSingleChain`); an unread one is `null` with
 * the reason in `graphError`, and `planShots` skips its linear shot.
 */
async function liveEpics(base) {
  const roster = await readJson(base, '/api/epics');
  if (!Array.isArray(roster.body)) {
    return { chosen: [], note: roster.error ?? '/api/epics did not answer a list' };
  }
  const dash = await readJson(base, '/api/dashboard');
  const pick = pickLiveEpics(roster.body, dash.body?.needs_you_by_epic ?? {});
  if (dash.error) pick.note = [pick.note, `needs-you not read: ${dash.error}`].filter(Boolean).join('; ');
  for (const c of pick.chosen) {
    const detail = await readJson(base, `/api/epics/${encodeURIComponent(c.ref)}`);
    const snapshot = detail.body?.snapshot;
    if (snapshot && Array.isArray(snapshot.nodes) && Array.isArray(snapshot.edges)) {
      c.linear = isSingleChain(snapshot);
    } else {
      c.linear = null;
      c.graphError = detail.error ?? 'no snapshot with nodes and edges';
    }
  }
  return pick;
}

async function main() {
  const argv = process.argv.slice(2);
  const [base, outArg] = argv.filter((a) => a !== '--live');
  if (!base) die('usage: node scripts/gallery.mjs <base-url> [out-dir] [--live]');
  const stamp = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
  const out = outArg ?? new URL(`../.shots/gallery/${stamp}/`, import.meta.url).pathname;
  mkdirSync(out, { recursive: true });

  // A server that is not there is one answer, not eighteen identical shots.
  // `vite dev` listens on `localhost`, which can be IPv6 only: use that name.
  try {
    await fetch(base, { signal: AbortSignal.timeout(5000) });
  } catch (e) {
    die(`${base} is not answering (${e.cause?.code ?? e.message}); start npm run dev:fixtures and pass its Local URL`);
  }

  // A named payload that is not on disk is one answer too, not a run of shots
  // that each silently fall back to the dev server's own set.
  for (const shot of SHOTS) {
    for (const file of Object.values(shot.fixtures ?? {})) {
      const path = fileURLToPath(new URL(file, FIXTURES));
      if (!existsSync(path)) die(`${shot.name} names a missing fixture: ${file}`);
    }
  }

  // Live mode skips every shot that would photograph fixture data — a route
  // naming a fixture id, or an override answered inside a live page — and the
  // manifest lists them, so a short live gallery says why it is short.
  const live = liveFlag(argv) || !(await servesFixtures(base));
  // The fixture epics are skipped live, so photograph the live roster's own.
  const epics = live ? await liveEpics(base) : null;
  if (epics) {
    for (const c of epics.chosen) console.log(`epic ${c.ref} (${c.why})`);
    if (epics.note) console.log(`epics: ${epics.note}`);
  }
  const plan = planShots(SHOTS, { live, epics: epics?.chosen ?? [] });
  for (const s of plan.skipped) console.log(`skip ${s.name} (${s.reason})`);

  const { chromium: browserType } = await playwright();
  const browser = await browserType.launch({ executablePath: chromium(), headless: true });
  const manifest = {
    base,
    mode: live ? 'live' : 'fixtures',
    taken: new Date().toISOString(),
    skipped: plan.skipped,
    ...(epics && { epics }),
    shots: []
  };
  try {
    for (const shot of plan.shots) {
      for (const size of WIDTHS) {
        const page = await browser.newPage({ viewport: { width: size.width, height: size.height } });
        const errors = [];
        page.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`));
        page.on('console', (m) => m.type() === 'error' && errors.push(`console: ${m.text()}`));
        const file = `${shot.name}.${size.label}.png`;
        const entry = { name: shot.name, route: shot.route, width: size.width, file, errors, steps: [] };
        if (shot.epic) entry.epic = shot.epic;
        // Recorded in the manifest: a reviewer reading a shot has to be able to
        // see that it was taken over a payload other than the committed set.
        if (shot.fixtures && !live) entry.fixtures = shot.fixtures;
        try {
          if (shot.fixtures && !live) await overlay(page, shot.fixtures);
          await page.goto(base + shot.route, { waitUntil: 'domcontentloaded', timeout: 20000 });
          await page.waitForTimeout(SETTLE_MS);
          for (const s of shot.then ?? []) {
            try {
              await step(page, s);
              entry.steps.push({ ...s, ok: true });
            } catch (e) {
              entry.steps.push({ ...s, ok: false, error: String(e.message).split('\n')[0] });
            }
          }
          entry.pageScrollsSideways = await page.evaluate(
            () => document.documentElement.scrollWidth > document.documentElement.clientWidth
          );
          await page.screenshot({ path: join(out, file) });
        } catch (e) {
          entry.errors.push(`load: ${String(e.message).split('\n')[0]}`);
        }
        manifest.shots.push(entry);
        console.log(`${errors.length ? 'ERR ' : 'ok  '} ${file}${errors.length ? ` (${errors.length} errors)` : ''}`);
        await page.close();
      }
    }
  } finally {
    await browser.close();
  }
  writeFileSync(join(out, 'manifest.json'), JSON.stringify(manifest, null, 2));
  console.log(`gallery (${manifest.mode}): ${manifest.shots.length} shots, ${plan.skipped.length} skipped -> ${out}`);
}

// Run only as a script: `scripts/gallery.test.ts` and the ui-check import the
// shot list without opening a browser.
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) main();
