/**
 * What `scripts/ui-check.mjs` and its surface modules share (#2570).
 *
 * The runner owns the browser, the dev server, `check()`, `visit()` and the
 * summary; each surface's checks live in `scripts/ui-checks/<name>.mjs`. This
 * file holds only what more than one of them reads, and the rule that finds the
 * modules, so a test can read that rule without starting a browser.
 */
import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { isDeepStrictEqual } from 'node:util';
import { DEMO_BANNER, DEMO_REFUSAL } from '../src/lib/demo/banner.js';
import { DEMO_SENTINEL } from '../src/lib/demo/sentinel.js';

export const FRONTEND = new URL('../', import.meta.url);

/** Where the surface modules live: one file per surface, run in name order. */
export const CHECKS_DIR = new URL('ui-checks/', import.meta.url);

/**
 * The folder the runner imports its modules from: `CHECKS_DIR`, unless
 * `$KEW_UI_CHECK_DIR` names another — which only a test of the runner itself
 * sets, to run it on a fixture module.
 */
export function checksDirFrom(env = process.env) {
  const named = env.KEW_UI_CHECK_DIR;
  return named ? pathToFileURL(named.endsWith('/') ? named : `${named}/`) : CHECKS_DIR;
}

/**
 * Every surface module in `dir`, in name order: each `.mjs` file there. The
 * runner imports exactly this list, so a new surface is a new file and never an
 * edit to the runner or to a sibling.
 */
export function checkModules(dir = CHECKS_DIR) {
  return readdirSync(dir)
    .filter((name) => name.endsWith('.mjs'))
    .sort();
}

/**
 * The ownership line a surface module carries for each file it checks (#3201):
 * `// ui-check-owns: src/routes/work/**`, a glob relative to `frontend/`. A
 * module's name says what it is about, not which files render it — `work-rows`
 * checks `src/lib/components/IssueList.svelte` — so a scoped run that read names
 * alone missed the module for a changed surface whose name differed. kew's
 * pre-review step reads the same line (`kew.ui_check_modules`), from the file's
 * text, never by importing it.
 */
export const OWNS_LINE = /^\s*(?:\/\/|\*)\s*ui-check-owns:\s*(\S+)\s*$/gm;

/** The globs one module's source says it owns. */
export function ownedGlobs(source) {
  return [...source.matchAll(OWNS_LINE)].map((match) => match[1]);
}

/**
 * Whether `path` (relative to `frontend/`) matches `glob`: `**` crosses
 * directories, `*` and `?` do not.
 */
export function globMatches(glob, path) {
  let pattern = '';
  for (let i = 0; i < glob.length; i += 1) {
    const char = glob[i];
    if (char === '*' && glob[i + 1] === '*') {
      const slash = glob[i + 2] === '/';
      pattern += slash ? '(?:.*/)?' : '.*';
      i += slash ? 2 : 1;
    } else if (char === '*') pattern += '[^/]*';
    else if (char === '?') pattern += '[^/]';
    else pattern += char.replace(/[.+^${}()|[\]\\]/g, '\\$&');
  }
  return new RegExp(`^${pattern}$`).test(path);
}

/** Every module in `dir` whose ownership lines match `path`, in name order. */
export function owningModules(path, names, dir = CHECKS_DIR) {
  const rel = path.replace(/^(?:\.\/)?(?:frontend\/)?/, '');
  return names.filter((name) =>
    ownedGlobs(readFileSync(new URL(name, dir), 'utf8')).some((glob) => globMatches(glob, rel))
  );
}

/**
 * The modules a scoped run makes (#2984): `npm run ui-check -- work-rows epic-rail`
 * runs only those surfaces, so a sandboxed agent checks what it touched without
 * the whole browser suite. A name may carry `.mjs` or not. A changed file's path
 * (anything with a `/`) selects every module that owns it (#3201), whatever that
 * module is called. No names is every module; a name with no module, or a path
 * no module owns, is refused, never silently skipped — a typo would otherwise
 * pass 0/0.
 */
export function selectModules(names, only = [], dir = CHECKS_DIR) {
  if (only.length === 0) return names;
  const paths = only.filter((entry) => entry.includes('/'));
  const wanted = only
    .filter((entry) => !entry.includes('/'))
    .map((name) => (name.endsWith('.mjs') ? name : `${name}.mjs`));
  const unknown = wanted.filter((name) => !names.includes(name));
  if (unknown.length > 0) {
    throw new Error(`no surface module named ${unknown.join(', ')} in scripts/ui-checks/`);
  }
  for (const path of paths) {
    const owners = owningModules(path, names, dir);
    if (owners.length === 0) {
      throw new Error(`no surface module owns ${path}: add a ui-check-owns line to the module that checks it`);
    }
    wanted.push(...owners);
  }
  return names.filter((name) => wanted.includes(name));
}

/**
 * Whether a missing Chromium fails the run instead of skipping it (#2984). A
 * scoped run is how a sandboxed agent checks the surfaces it touched; a skip
 * there would report success for modules no browser loaded, so naming a module
 * demands a browser just as `KEW_UI_CHECK_REQUIRE_CHROMIUM=1` does.
 */
export function chromiumRequired(only = [], env = process.env) {
  return only.length > 0 || env.KEW_UI_CHECK_REQUIRE_CHROMIUM === '1';
}

/**
 * The two phases a run makes (#3139). A module named `demo-*.mjs` checks the
 * static demo build and runs only against a server for `build-demo/`; every
 * other module checks the fixture-mode dev server and runs only against that.
 */
export const DEMO_PREFIX = 'demo-';

/** Whether `name` is a demo module: one that runs against the static demo build. */
export const isDemoModule = (name) => name.startsWith(DEMO_PREFIX);

/** The modules of one phase — `'demo'`, `'dev'` — or every module when no phase is named. */
export function inPhase(names, phase) {
  if (phase === undefined) return names;
  return names.filter((name) => isDemoModule(name) === (phase === 'demo'));
}

/**
 * Whether this run builds the demo (#3139): the default run always does, and a
 * scoped run only when it names a demo module — a scoped run is how a sandbox
 * checks what it touched, and a build it does not need is minutes it loses.
 */
export function demoBuildNeeded(only = []) {
  return only.length === 0 || only.some((name) => isDemoModule(name));
}

/**
 * `--builds` (#3213): a scoped run that still makes both builds, as a full run
 * does. kew's scoped merge verify passes it with the modules a change owns, so
 * the frontend build and the demo build stay whole while the browser checks
 * shrink to that list. Given with no module it runs the builds and no module.
 */
export const BUILDS_FLAG = '--builds';

/** What a run's arguments ask for: the modules or paths named, and `--builds`. */
export function runScope(argv = []) {
  return { only: argv.filter((arg) => arg !== BUILDS_FLAG), builds: argv.includes(BUILDS_FLAG) };
}

/** The modules a run makes: none for a bare `--builds`, else `selectModules`. */
export function scopedModules(names, { only, builds }, dir = CHECKS_DIR) {
  if (builds && only.length === 0) return [];
  return selectModules(names, only, dir);
}

/** Which builds a run makes: a full run or `--builds` makes both. */
export function buildsToRun({ only, builds }) {
  return { build: builds || only.length === 0, demo: builds || demoBuildNeeded(only) };
}

/**
 * Import every surface module in `dir` (or just those `only` names, or just one
 * phase's) and run its `checks(args)`, in name order. Returns the files it ran,
 * so a test can see what was discovered.
 */
export async function runChecks(args, dir = CHECKS_DIR, only = [], phase = undefined) {
  const found = checkModules(dir);
  // An empty folder would print 0/0 and pass: that is a lost folder, not a pass.
  if (found.length === 0) throw new Error(`no surface modules in ${fileURLToPath(dir)}`);
  const names = inPhase(selectModules(found, only, dir), phase);
  const ran = [];
  for (const name of names) {
    const module = await import(new URL(name, dir).href);
    if (typeof module.checks !== 'function') {
      throw new Error(`ui-checks/${name} exports no checks() function`);
    }
    await module.checks(args);
    ran.push(name);
  }
  return ran;
}

/** The title of the failing result an error outside any check is recorded under. */
export const OUTSIDE_CHECK = 'ui-check: an error outside any check';

const reasonText = (reason) => String(reason?.message ?? reason);

/** How long one check may run before it fails: past the 60s health beat, the
 * longest a check waits on purpose, with room for a slow host. */
export const CHECK_DEADLINE_MS = 300_000;

/**
 * The runner's `check()`, `skip()` and the results its summary counts (#3477).
 *
 * Playwright calls a `page.route` handler from an event listener and never
 * catches its promise, so a handler that throws is an unhandled rejection, and
 * Node's default for one is to exit: the run ended with no summary and the
 * checks after it never ran. The recorder listens for `unhandledRejection` on
 * `events` from the moment it is made. One that arrives while a check's body
 * runs fails that check; one that arrives between checks is a failing result
 * of its own. Neither exits.
 *
 * Checks can overlap (`hidden-tab.mjs` runs a pool of them at once), and a
 * rejection carries nothing that says which check's page raised it, so one
 * that arrives while several checks run fails every one of them, each told
 * which others were running. A rejection is attributed to the checks running
 * when it is observed, which need not be the check that caused it, or, with
 * none running, to an outside-check failure; a check that has ended is never
 * written to.
 *
 * A check whose body has not settled after `deadlineMs` fails, and the run
 * goes on: a body that awaits something nothing will answer must not hold the
 * run until kew verify's ceiling. Its body is left running, unawaited.
 */
export function checkRecorder({ events = process, log = console.log, deadlineMs = CHECK_DEADLINE_MS } = {}) {
  const results = [];
  const skipped = [];
  /** Every check whose body is running, with the errors it has collected. */
  const running = new Set();

  function fail(name, message) {
    results.push({ name, ok: false, message });
    log(`  FAIL  ${name}`);
    for (const line of message.split('\n')) log(`        ${line}`);
  }

  events.on('unhandledRejection', (reason) => {
    const message = `a route or page handler threw: ${reasonText(reason)}`;
    if (running.size === 0) return fail(OUTSIDE_CHECK, message);
    for (const each of running) {
      const others = [...running].filter((other) => other !== each).map((other) => other.name);
      each.thrown.push(others.length ? `${message} (while ${others.join(', ')} also ran)` : message);
    }
  });

  /** A check this run cannot make, printed by name with why: never a silent pass. */
  function skip(name, why) {
    skipped.push(name);
    log(`  skip  ${name} (${why})`);
  }

  /** Settles when `body` does, or rejects once `deadlineMs` has passed. */
  function withinDeadline(body) {
    let timer;
    const deadline = new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error(`the check did not finish within ${deadlineMs} ms`)), deadlineMs);
    });
    return Promise.race([Promise.resolve().then(body), deadline]).finally(() => clearTimeout(timer));
  }

  async function check(name, body) {
    const thrown = [];
    const self = { name, thrown };
    running.add(self);
    try {
      await withinDeadline(body);
    } catch (error) {
      thrown.push(String(error.message));
    } finally {
      running.delete(self);
    }
    if (thrown.length === 0) {
      results.push({ name, ok: true });
      log(`  ok    ${name}`);
    } else {
      fail(name, thrown.join('\n'));
    }
  }

  return { results, skipped, skip, check };
}

/** What a route handler that threw is settled with, so the page is not left waiting. */
const SETTLED = Symbol('ui-check: route handlers settle when they throw');

/**
 * Make every `route()` on `target`'s prototype settle its route when the
 * handler throws (#3477). Playwright leaves a route whose handler threw
 * unanswered, so a check awaiting that request in the page waited forever;
 * `fetchThrough` aborts only the routes that go through it. Here the
 * handler's route is aborted and the error thrown on, so the recorder still
 * fails the check with it. Patched on the prototype, once: the pages and
 * contexts a module makes for itself are settled too. `unroute` is mapped
 * back to the wrapped handler so removing a route by its handler still works.
 */
export function settleThrownRoutes(target) {
  const proto = Object.getPrototypeOf(target);
  if (proto[SETTLED]) return;
  const { route: addRoute, unroute: removeRoute } = proto;
  const wrapped = new WeakMap();
  proto.route = function (url, handler, options) {
    const settled = async (route, request) => {
      try {
        return await handler(route, request);
      } catch (error) {
        await route.abort().catch(() => {});
        throw error;
      }
    };
    wrapped.set(handler, settled);
    return addRoute.call(this, url, settled, options);
  };
  proto.unroute = function (url, handler) {
    return removeRoute.call(this, url, handler && (wrapped.get(handler) ?? handler));
  };
  proto[SETTLED] = true;
}

/**
 * Guard the run's exit (#3477): a module that calls `process.exit(0)` ended
 * the run with exit 0 and no summary, a pass no check made. Until `done()` is
 * called — just before the summary's own exit — an exit with code 0 is turned
 * into exit 1, with a FAIL line saying why.
 */
export function exitGuard({ events = process, log = console.log } = {}) {
  let reached = false;
  events.on('exit', (code) => {
    if (reached || code !== 0) return;
    log('  FAIL  ui-check exited before its summary');
    log('        a module called process.exit(0): no summary counted the checks');
    process.exitCode = 1;
  });
  return { done: () => (reached = true) };
}

/** What a dev server that is still starting answers a fetch with: worth another try. */
const TRANSIENT = /ECONNRESET|ECONNREFUSED|socket hang up/;
const FETCH_TRIES = 3;
const FETCH_GAP_MS = 250;

/**
 * `route.fetch(options)` for a `page.route` handler, tried again on a reset
 * (#3477): a cold vite dev server once answered one with `ECONNRESET`. Any
 * other error, or a third reset, aborts the route — so the page is not left
 * waiting for an answer — and throws a message naming the URL and the tries.
 */
export async function fetchThrough(route, options) {
  for (let tries = 1; ; tries += 1) {
    try {
      return await route.fetch(options);
    } catch (error) {
      const message = reasonText(error);
      if (tries < FETCH_TRIES && TRANSIENT.test(message)) {
        await new Promise((resolve) => setTimeout(resolve, FETCH_GAP_MS));
        continue;
      }
      await route.abort().catch(() => {});
      const url = route.request().url();
      throw new Error(`route.fetch ${url} failed after ${tries} ${tries === 1 ? 'try' : 'tries'}: ${message}`);
    }
  }
}


/**
 * The build history the demo at `base` serves (`/demo-provenance.json`,
 * `PROVENANCE_ASSET`), and whether it is the real export (#3207): the demo
 * build pins every PR link of the real file to `demo/public-repo.json`'s
 * repository and every sample link to the invented one, so the links say
 * which. Read off the served file: a deployed build can carry the real file
 * while the checkout holds only the samples, so a `remote` base is taken at
 * its word. A local base is the checkout's own build, so there the served
 * file must also equal the one the checkout selects — `provenance.json` in
 * `dir` when it exists, else `provenance.sample.json`, the rule of
 * `provenanceFile` in `demo/provenance-bundle.ts` — or a build that shipped
 * the wrong file would be checked against itself and pass. The file is read
 * from the base itself: a redirect anywhere is refused.
 */
export async function servedProvenance(base, { remote = false, dir = new URL('demo/provenance/', FRONTEND) } = {}) {
  const at = `${base}/demo-provenance.json`;
  let response;
  try {
    response = await fetch(at, { redirect: 'error' });
  } catch (error) {
    throw new Error(`${at} could not be read from the base (a redirect is refused): ${error.cause?.message ?? error.message}`);
  }
  if (new URL(response.url).origin !== new URL(base).origin) throw new Error(`${at} was answered from ${response.url}`);
  if (!response.ok) throw new Error(`${at} answered ${response.status}`);
  const doc = await response.json();
  if (!remote) {
    const realFile = new URL('provenance.json', dir);
    const selected = existsSync(realFile) ? realFile : new URL('provenance.sample.json', dir);
    if (!isDeepStrictEqual(doc, JSON.parse(readFileSync(selected, 'utf8')))) {
      throw new Error(`${at} is not ${fileURLToPath(selected)}, the file this checkout's demo build ships`);
    }
  }
  const { repo } = JSON.parse(readFileSync(new URL('demo/public-repo.json', FRONTEND), 'utf8'));
  const urls = doc.features.flatMap((feature) => feature.prs.map((pr) => pr.url));
  const real = urls.length > 0 && urls.every((url) => url.startsWith(`https://github.com/${repo}/pull/`));
  return { real, doc };
}

/** The normal build `kew serve` ships, and the static demo build (#3139). */
export const BUILD_DIR = new URL('build/', FRONTEND);
export const DEMO_BUILD_DIR = new URL('build-demo/', FRONTEND);

/**
 * What must never be in the normal build (#3139), each with why:
 *   - the demo flag's sentinel, which the demo shim stamps on the document —
 *     present means the shim survived the tree-shake;
 *   - the banner's sentence and the start of every refused write's sentence,
 *     the words the other demo modules carry — present means one of them was
 *     imported by code the normal build keeps (the import boundary in
 *     `src/lib/demo/boundary.test.ts` refuses the import itself);
 *   - driver.js's shipped class name, because the tours are demo-only;
 *   - a fixture-only run id, the sentinel `src/lib/fixtures.test.ts` greps a
 *     present build for — present means fixture data was bundled.
 */
export function buildSentinels() {
  const runs = JSON.parse(readFileSync(new URL('fixtures/factory/runs.json', FRONTEND), 'utf8'));
  return [
    { sentinel: DEMO_SENTINEL, what: 'the demo flag sentinel' },
    { sentinel: DEMO_BANNER, what: "the demo banner's sentence" },
    { sentinel: DEMO_REFUSAL, what: "a demo refusal's sentence" },
    { sentinel: 'driver-popover', what: "driver.js's shipped class name" },
    { sentinel: runs[0].run_id, what: 'a fixture-only run id' }
  ];
}

/**
 * The environment `npm run <script>` runs under: this process's, except that
 * the normal build never sees `KEW_DEMO_BUILD` — with it set, `svelte.config.js`
 * writes `build-demo/`, and the leak check would grep a stale `build/`.
 */
export function npmEnv(script, env = process.env) {
  const out = { ...env };
  if (script === 'build') delete out.KEW_DEMO_BUILD;
  return out;
}

/** Every file under `dir`. */
function filesUnder(dir) {
  return readdirSync(dir).sort().flatMap((name) => {
    const path = join(dir, name);
    return statSync(path).isDirectory() ? filesUnder(path) : [path];
  });
}

/**
 * Each sentinel found in a file under `dir`, as `<file>: <what> (<sentinel>)`.
 * Empty is the only passing answer.
 */
export function buildLeaks(dir = BUILD_DIR, sentinels = buildSentinels()) {
  const root = fileURLToPath(dir);
  const leaks = [];
  for (const file of filesUnder(root)) {
    const text = readFileSync(file, 'latin1');
    for (const { sentinel, what } of sentinels) {
      if (text.includes(sentinel)) leaks.push(`${file.slice(root.length)}: ${what} (${sentinel})`);
    }
  }
  return leaks;
}

/** The artboards' viewport, same as `shoot.mjs` — the size the design is drawn at. */
export const WIDTH = 1440;
export const HEIGHT = 960;

/** The fixture epic most surfaces read. */
export const EPIC = '/epics/launch-fixes';

/** How long a page is given to settle before its console is judged quiet. */
export const SETTLE_MS = 700;

/**
 * The page's scrollable width against its viewport, and the box that set it.
 *
 * `scrollWidth > clientWidth` is the whole assertion, but on its own it says
 * only that something is too wide — the first time this went red it took a
 * browser session to learn the epic page was blameless and the shell header was
 * the box (#2419, the full right cluster at 641px). So the reading comes with
 * the widest element that reaches past the viewport, named by tag and by the
 * utilities it carries. Clipped and hidden boxes are skipped: they are not what
 * a page can be dragged sideways to find.
 */
export function widestBox(page) {
  return page.evaluate(() => {
    const doc = document.documentElement;
    let worst = null;
    for (const el of document.querySelectorAll('body *')) {
      const style = getComputedStyle(el);
      if (style.visibility === 'hidden' || style.display === 'none') continue;
      const box = el.getBoundingClientRect();
      if (box.right <= doc.clientWidth + 1) continue;
      // A box inside something that clips is not draggable-to, so it is not the
      // reason the PAGE scrolls — only an unclipped ancestor chain counts.
      let clipped = false;
      for (let p = el.parentElement; p && !clipped; p = p.parentElement) {
        const o = getComputedStyle(p).overflowX;
        clipped = o === 'hidden' || o === 'clip' || o === 'auto' || o === 'scroll';
      }
      if (clipped) continue;
      if (worst === null || box.right > worst.right) {
        worst = {
          right: box.right,
          name: `<${el.tagName.toLowerCase()} class="${String(el.className).slice(0, 60)}">`
        };
      }
    }
    return {
      w: doc.scrollWidth,
      c: doc.clientWidth,
      culprit: worst ? `${worst.name} to ${Math.round(worst.right)}px` : 'none found'
    };
  });
}

/**
 * Throw unless `locator`'s text is painted where a person can read it (#2878).
 *
 * DOM text and a button's box say nothing about whether the words show: a
 * transparent colour, a zero-opacity ancestor, a clipping parent or a box laid
 * over the top all leave `textContent` intact. Nothing is scrolled: scrolling
 * the target into view would carry a label laid out past the right edge back
 * on screen and pass it. So the page is read where it stands — bring the target
 * into view vertically before calling — and passes only when all of these hold:
 *   - the document does not overflow sideways, and neither the window nor any
 *     ancestor of the element is scrolled sideways;
 *   - its text has a non-zero rendered rectangle inside the viewport;
 *   - its computed colour is not transparent, it is not `visibility: hidden`,
 *     and neither it nor any ancestor has zero opacity;
 *   - no box that clips (`overflow` other than `visible`: `hidden`, `clip`,
 *     `auto`, `scroll`) cuts any of its text's line rectangles — the target's
 *     own box included, so a button narrower than its label fails;
 *   - `elementFromPoint` at the rectangle's centre is the element or inside it,
 *     the element taking hits for that probe even when it is `pointer-events:
 *     none` (a disabled button), so only a box truly on top fails it;
 *   - its text contrasts at least `MIN_CONTRAST` (WCAG AA) with the ground it
 *     is painted on, both composited as the browser paints them: every
 *     ancestor's background colour layered over the canvas, and each node's
 *     opacity applied to its whole group — so a 45% disabled fade counts;
 *   - no scroll position moved while it was measured.
 * It is measured once no CSS transition on it, an ancestor or its contents is
 * still running, so the reading is the frame that stays, not one in flight.
 * The error names the first one that failed.
 */
export const MIN_CONTRAST = 4.5;

export async function assertPainted(page, locator, what = 'the element') {
  // A computed style read mid-transition is the frame in flight, not the one
  // that stays: a button with `transition-[…color…]` reports its old colour
  // for 150ms after a change — a transparent control read too soon passes,
  // and a Run turning from neutral to magenta reads as low contrast. So the
  // transitions on the target, its ancestors and its contents end first.
  const unsettled = await locator.evaluate(async (el, ms) => {
    const running = () =>
      document
        .getAnimations()
        .filter((a) => a instanceof CSSTransition && a.playState === 'running')
        .filter((a) => a.effect?.target instanceof Element)
        .filter((a) => a.effect.target.contains(el) || el.contains(a.effect.target));
    const deadline = performance.now() + ms;
    for (let left = running(); left.length > 0; left = running()) {
      if (performance.now() > deadline) return left.map((a) => a.transitionProperty).join(', ');
      const ended = Promise.all(left.map((a) => a.finished.catch(() => null)));
      await Promise.race([ended, new Promise((r) => setTimeout(r, 100))]);
    }
    return null;
  }, SETTLE_MS * 3);
  if (unsettled !== null) throw new Error(`${what} is not painted: it is still in transition (${unsettled})`);
  const problem = await locator.evaluate((el, least) => {
    const doc = document.documentElement;
    const scrollers = [];
    for (let node = el.parentElement; node instanceof Element; node = node.parentElement) scrollers.push(node);
    const positions = () =>
      JSON.stringify([window.scrollX, window.scrollY, ...scrollers.map((n) => [n.scrollLeft, n.scrollTop])]);
    const before = positions();
    const measure = () => {
      if (doc.scrollWidth > doc.clientWidth + 1) {
        return `the document overflows sideways (${doc.scrollWidth}px in a ${doc.clientWidth}px viewport)`;
      }
      if (window.scrollX !== 0) return `the window is scrolled sideways by ${window.scrollX}px`;
      const shifted = scrollers.find((n) => n.scrollLeft !== 0);
      if (shifted) {
        return `<${shifted.tagName.toLowerCase()}> is scrolled sideways by ${shifted.scrollLeft}px`;
      }
      const range = document.createRange();
      range.selectNodeContents(el);
      const r = range.getBoundingClientRect();
      if (r.width <= 0 || r.height <= 0) return 'its text has no rendered rectangle';
      const vw = doc.clientWidth;
      const vh = doc.clientHeight;
      if (r.left < 0 || r.top < 0 || r.right > vw + 0.5 || r.bottom > vh + 0.5) {
        return `its text at ${JSON.stringify(r)} is not inside the ${vw}x${vh} viewport`;
      }
      const style = getComputedStyle(el);
      if (style.visibility !== 'visible') return `it is visibility: ${style.visibility}`;
      // Alpha is the fourth part of `rgba(r, g, b, a)` or what follows the `/`
      // of a colour-4 form (`oklch(l c h / a)`); an opaque colour carries none.
      const alpha =
        /\/\s*([\d.]+)%?\s*\)$/.exec(style.color) ??
        /^rgba\([^,]+,[^,]+,[^,]+,\s*([\d.]+)%?\s*\)$/.exec(style.color);
      const a = alpha ? parseFloat(alpha[1]) : 1;
      if (style.color === 'transparent' || a === 0) return `its colour is ${style.color}`;
      // Every line of the text, not their union: a wrapped label can have one
      // line cut while the union still fits.
      const lines = [...range.getClientRects()].filter((line) => line.width > 0 && line.height > 0);
      for (let node = el; node instanceof Element; node = node.parentElement) {
        const s = getComputedStyle(node);
        if (parseFloat(s.opacity) === 0) return `<${node.tagName.toLowerCase()}> has opacity 0`;
        if (!clipsBox(node, s)) continue;
        // The padding box is what overflow cuts at, and the target's own box
        // counts too: a button narrower than its label clips the label itself.
        const b = node.getBoundingClientRect();
        const left = b.left + node.clientLeft;
        const top = b.top + node.clientTop;
        const right = left + node.clientWidth;
        const bottom = top + node.clientHeight;
        const cut = lines.some(
          (line) =>
            line.left < left - 0.5 || line.top < top - 0.5 || line.right > right + 0.5 || line.bottom > bottom + 0.5
        );
        if (cut) return `<${node.tagName.toLowerCase()} class="${String(node.className).slice(0, 60)}"> clips its text`;
      }
      // Hit testing skips a `pointer-events: none` box, and a disabled control
      // is one (`disabled:pointer-events-none`): the point would land on its
      // parent and read as covered. Paint is the question, not clicks, so the
      // target takes hits for the one probe; a box truly on top still wins.
      const events = [el.style.getPropertyValue('pointer-events'), el.style.getPropertyPriority('pointer-events')];
      el.style.setProperty('pointer-events', 'auto', 'important');
      const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
      el.style.setProperty('pointer-events', ...events);
      if (hit === null || !el.contains(hit)) {
        return `${hit ? `<${hit.tagName.toLowerCase()}>` : 'nothing'} is on top of its text`;
      }
      const ratio = contrast();
      if (ratio < least) {
        return `its text contrasts ${ratio.toFixed(2)}:1 with its ground, under ${least}:1`;
      }
      return null;
    };
    /** Whether `node`'s own box cuts what overflows it. Every overflow but
     * `visible` does: `auto` and `scroll` hide the rest behind a scroll as
     * surely as `hidden` and `clip` drop it. The root's overflow belongs to the
     * viewport (measured above), and so does the body's when the root leaves
     * its own visible; an inline box takes no overflow at all. */
    const clipsBox = (node, s) => {
      if (node === doc || s.display === 'inline' || s.display === 'contents') return false;
      if (node === document.body) {
        const root = getComputedStyle(doc);
        if (root.overflowX === 'visible' && root.overflowY === 'visible') return false;
      }
      return s.overflowX !== 'visible' || s.overflowY !== 'visible';
    };
    /** Any CSS colour (oklch, color-mix, …) as sRGB 0–255 and alpha 0–1, read
     * back from a one-pixel canvas the way the browser resolves it. */
    const canvas = document.createElement('canvas');
    canvas.width = canvas.height = 1;
    const pen = canvas.getContext('2d', { willReadFrequently: true });
    const rgba = (css) => {
      pen.clearRect(0, 0, 1, 1);
      pen.fillStyle = '#000';
      pen.fillStyle = css;
      pen.fillRect(0, 0, 1, 1);
      const [cr, cg, cb, ca] = pen.getImageData(0, 0, 1, 1).data;
      const a = ca / 255;
      return a === 0 ? [0, 0, 0, 0] : [cr / 255, cg / 255, cb / 255, a];
    };
    // Premultiplied source-over, so a group's opacity scales it whole.
    const pre = ([cr, cg, cb, a]) => [cr * a, cg * a, cb * a, a];
    const over = (top, under) => top.map((v, i) => v + under[i] * (1 - top[3]));
    const fade = (c, o) => c.map((v) => v * o);
    /** The text pixel and a ground pixel beside it, as painted (#2878). */
    const contrast = () => {
      const chain = [];
      for (let node = el; node instanceof Element; node = node.parentElement) chain.unshift(node);
      // Each group is drawn on its own, then laid into its parent at its opacity.
      const group = (i, text) => {
        const s = getComputedStyle(chain[i]);
        let painted = pre(rgba(s.backgroundColor));
        const inner = i + 1 < chain.length ? group(i + 1, text) : text ? pre(rgba(s.color)) : null;
        if (inner !== null) painted = over(inner, painted);
        return fade(painted, parseFloat(s.opacity));
      };
      // The root's own background is in the chain; under it, a white canvas.
      const base = [1, 1, 1, 1];
      const ink = over(group(0, true), base);
      const ground = over(group(0, false), base);
      const lum = ([cr, cg, cb]) => {
        const lin = (v) => (v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4);
        return 0.2126 * lin(cr) + 0.7152 * lin(cg) + 0.0722 * lin(cb);
      };
      const [lo, hi] = [lum(ink), lum(ground)].sort((x, y) => x - y);
      return (hi + 0.05) / (lo + 0.05);
    };
    const found = measure();
    if (found !== null) return found;
    const after = positions();
    return after === before ? null : `a scroll position moved while it was measured (${before} → ${after})`;
  }, MIN_CONTRAST);
  if (problem !== null) throw new Error(`${what} is not painted: ${problem}`);
}

// ---------------------------------------------------------------------------
// The demo tours' checks (`demo-tour-*.mjs`): a tour's words read straight
// from its docs passage, where its target is drawn, and the step on show.

/** Where the docs live; a tour step's passage is read straight from the markdown. */
const TOUR_DOCS = new URL('../docs/', FRONTEND);

/** The tour popover driver.js draws. */
export const TOUR_POPOVER = '.driver-popover.kew-tour';

export const squash = (text) => text.replace(/\s+/g, ' ').trim();

/** Markdown as the popover shows it: code spans and bold unwrapped, a link's text only. */
const shown = (markdown) =>
  squash(
    markdown
      .replace(/`([^`]+)`/g, '$1')
      .replace(/\*\*([^*]+)\*\*/g, '$1')
      .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
  );

/** Passage `id` of a docs page, as `{ title, body: [paragraph, ...] }`. */
export function docsPassage(page, id) {
  const lines = readFileSync(new URL(page, TOUR_DOCS), 'utf8').split('\n');
  const open = lines.findIndex((line) => line.trim() === `<!-- demo-step: ${id} -->`);
  if (open < 0) throw new Error(`docs/${page} has no demo-step ${id}`);
  const close = lines.findIndex((line, i) => i > open && line.trim() === '<!-- /demo-step -->');
  const [head, ...rest] = lines.slice(open + 1, close).join('\n').trim().split('\n');
  const title = /^\*\*(.+)\*\*$/.exec(head.trim())?.[1];
  if (!title) throw new Error(`docs/${page} demo-step ${id} has no **title** line`);
  const body = rest.join('\n').split(/\n\s*\n/).map(shown).filter((p) => p !== '');
  return { title: shown(title), body };
}

/**
 * Where `selector` is drawn once it stops moving (a scroll, or driver.js's own
 * animation), and the part of the window that can show it: the window cut
 * down by every ancestor that clips (overflow other than visible), each at its
 * padding box. `inside` is the whole box within that area, to the pixel.
 */
export async function placement(page, selector) {
  return page.evaluate(async (sel) => {
    const frame = () => new Promise((resolve) => requestAnimationFrame(() => resolve()));
    const read = () => {
      const r = document.querySelector(sel)?.getBoundingClientRect();
      return r ? [r.left, r.top, r.width, r.height].map(Math.round).join(',') : 'none';
    };
    let last = read();
    let still = 0;
    for (const until = performance.now() + 4_000; performance.now() < until && still < 10; ) {
      await frame();
      const now = read();
      still = now === last ? still + 1 : 0;
      last = now;
    }
    const el = document.querySelector(sel);
    if (!el) return { found: false };
    const r = el.getBoundingClientRect();
    const clip = { top: 0, left: 0, bottom: innerHeight, right: innerWidth };
    const by = [];
    for (let a = el.parentElement; a && a !== document.documentElement; a = a.parentElement) {
      const cs = getComputedStyle(a);
      if (cs.overflowX === 'visible' && cs.overflowY === 'visible') continue;
      const b = a.getBoundingClientRect();
      const top = b.top + a.clientTop;
      const left = b.left + a.clientLeft;
      if (top > clip.top || top + a.clientHeight < clip.bottom) {
        const label = a.getAttribute('aria-label');
        by.push(label ? `${a.tagName.toLowerCase()}[${label}]` : a.tagName.toLowerCase());
      }
      clip.top = Math.max(clip.top, top);
      clip.left = Math.max(clip.left, left);
      clip.bottom = Math.min(clip.bottom, top + a.clientHeight);
      clip.right = Math.min(clip.right, left + a.clientWidth);
    }
    const inside =
      r.left >= clip.left - 1 && r.top >= clip.top - 1 && r.right <= clip.right + 1 && r.bottom <= clip.bottom + 1;
    return {
      found: true,
      empty: r.width < 1 || r.height < 1,
      inside,
      box: [r.left, r.top, r.right, r.bottom].map(Math.round),
      area: [clip.left, clip.top, clip.right, clip.bottom].map(Math.round),
      by
    };
  }, selector);
}

/** Fail unless `selector` is drawn with a box, all of it on screen. */
export async function onScreen(page, expect, selector, what) {
  const at = await placement(page, selector);
  expect(at.found, `${what}: nothing matches ${selector}`);
  expect(!at.empty, `${what}: ${selector} is drawn as an empty box at [l,t,r,b] ${JSON.stringify(at.box)}`);
  expect(
    at.inside,
    `${what}: ${selector} at [l,t,r,b] ${JSON.stringify(at.box)} is cut off; the area that shows it is ` +
      `${JSON.stringify(at.area)} (clipped by ${at.by.join(' > ') || 'the window'})`
  );
}

/**
 * The band a scrolling body with more below its fold fades out over, when its
 * mask cannot be read: the node popover's `.pbody.more` rule in
 * `NodePopover.svelte`, `linear-gradient(to bottom, #000 calc(100% - 28px),
 * transparent)`. `clearOfFade` reads the band off the computed mask first.
 */
export const FADE_BAND = 28;

/**
 * Fail when `selector` sits in a body that fades its foot (the nearest
 * `[data-more]` ancestor, `data-more="true"` while there is more below the
 * fold, with a mask drawn) and any of it reaches into that fade: its bottom
 * must be at or above the body's bottom less the band. The band is read off
 * the body's computed `mask-image` (`calc(100% - <n>px)`), else `FADE_BAND`.
 * A target in no such body, or in one with nothing below its fold or no mask
 * (a focus inside lifts it), passes.
 */
export async function clearOfFade(page, expect, selector, what) {
  const at = await page.evaluate(
    ([sel, fallback]) => {
      const el = document.querySelector(sel);
      const body = el?.closest('[data-more]');
      if (!el || !body) return null;
      const style = getComputedStyle(body);
      const mask = style.maskImage || style.webkitMaskImage || 'none';
      const band = Number(/calc\(100% - ([\d.]+)px\)/.exec(mask)?.[1] ?? fallback);
      return {
        more: body.getAttribute('data-more') === 'true' && mask !== 'none',
        band,
        bottom: el.getBoundingClientRect().bottom,
        limit: body.getBoundingClientRect().bottom - band
      };
    },
    [selector, FADE_BAND]
  );
  if (at === null || !at.more) return;
  expect(
    at.bottom <= at.limit + 0.5,
    `${what}: ${selector}'s bottom at ${Math.round(at.bottom)} reaches into the ${at.band}px fade that starts at ${Math.round(at.limit)}`
  );
}

/**
 * The selector of what a tour step highlights, as the controller builds it
 * (`stepSelector` in `src/lib/demo/tours/controller.ts`): its `data-tour`
 * target, or the graph card it names inside that target.
 */
export function stepSelector(step) {
  const target = `[data-tour="${step.target}"]`;
  return step.card === undefined ? target : `${target} .svelte-flow__node[data-id="${step.card}"]`;
}

/**
 * Wait until the popover shows passage `want` with `target` highlighted:
 * a step (its `stepSelector`), or a `data-tour` value.
 */
export async function showing(page, want, target) {
  const selector = typeof target === 'string' ? `[data-tour="${target}"]` : stepSelector(target);
  try {
    await page.waitForFunction(
      ([sel, w]) => document.querySelector(sel)?.getAttribute('data-tour-step') === w,
      [TOUR_POPOVER, want],
      { timeout: 15_000 }
    );
  } catch {
    throw new Error(`the popover never showed ${want}`);
  }
  try {
    await page.waitForFunction(
      (sel) => document.querySelector(sel)?.classList.contains('driver-active-element'),
      selector,
      { timeout: 10_000 }
    );
  } catch {
    const drawn = await page.locator(selector).count();
    throw new Error(`${want} never highlighted ${selector} (${drawn} drawn on ${new URL(page.url()).pathname})`);
  }
}

/** The sizes a popover must keep clear of what it explains (design review c9, D4 and D9). */
export const CLEAR_SIZES = [
  { width: 1440, height: 960 },
  { width: 1366, height: 650 }
];

/**
 * Fail unless the tour popover covers neither the step's highlighted target
 * nor the page dialog the target sits in (the node popover whose runs a step
 * reads), nor the graph card the step's `?node=` names, nor the region it
 * names as `clear`, polled for a moment: a step re-places a frame after it
 * settles.
 */
export async function popoverCoversNothing(page, expect, step, at) {
  const read = () =>
    page.evaluate(
      ([pop, sel, clear, node]) => {
        const box = (el) => {
          const r = el.getBoundingClientRect();
          return { left: r.left, top: r.top, right: r.right, bottom: r.bottom };
        };
        const popover = document.querySelector(pop);
        const target = document.querySelector(sel);
        if (!popover || !target) return { found: false };
        const dialog = target.parentElement?.closest('[role="dialog"]');
        const avoid = [['its target', box(target)]];
        if (dialog && !dialog.classList.contains('driver-popover')) avoid.push(['the dialog its target is in', box(dialog)]);
        // The graph card the step's `?node=` names, when it is not the target.
        const card = node ? document.querySelector(`[data-tour="epic-graph"] .svelte-flow__node[data-id="${node}"]`) : null;
        if (card && card !== target) avoid.push([`the card it names, #${node}`, box(card)]);
        // The region the step's sentence compares across (its `clear`).
        const region = clear ? document.querySelector(`[data-tour="${clear}"]`) : null;
        if (clear) {
          if (!region) return { found: false };
          avoid.push([`the region it compares, [data-tour="${clear}"]`, box(region)]);
        }
        const p = box(popover);
        const over = (a, b) => !(a.bottom <= b.top || a.top >= b.bottom || a.right <= b.left || a.left >= b.right);
        return {
          found: true,
          popover: [p.left, p.top, p.right, p.bottom].map(Math.round),
          covered: avoid.filter(([, b]) => over(p, b)).map(([name, b]) => `${name} at ${JSON.stringify([b.left, b.top, b.right, b.bottom].map(Math.round))}`)
        };
      },
      [TOUR_POPOVER, stepSelector(step), step.clear ?? null, step.query?.node ?? null]
    );
  let seen = await read();
  for (const until = Date.now() + 3_000; Date.now() < until && seen.found && seen.covered.length > 0; ) {
    await page.waitForTimeout(100);
    seen = await read();
  }
  expect(seen.found, `${at}: no popover, or no ${stepSelector(step)}`);
  expect(
    seen.covered.length === 0,
    `${at}: the tour popover at [l,t,r,b] ${JSON.stringify(seen.popover)} covers ${seen.covered.join(' and ')}`
  );
}

/**
 * The desktop sizes a tour runs at, start to end: a wide window and the two
 * short laptops (tours start at 1024 wide).
 */
export const TOUR_SIZES = [
  { width: 1440, height: 960 },
  { width: 1366, height: 650 },
  { width: 1024, height: 768 }
];

export const sizeName = ({ width, height }) => `${width}x${height}`;

/** The smallest a graph card's title may render at, in CSS pixels: the type scale's floor. */
export const MIN_CARD_TEXT_PX = 11;

/**
 * Fail unless graph card `id` is drawn wholly inside the graph's node area
 * (`.floor`) and its title renders at `MIN_CARD_TEXT_PX` or more, its font
 * size times the canvas zoom, and nothing but the tour's dimming overlay is
 * drawn over its title (no tour popover, no node popover): a step that names a
 * card asks the visitor to read it (design review c9).
 */
export async function cardReadable(page, expect, id, at) {
  const seen = await page.evaluate((node) => {
    const floor = document.querySelector('[data-tour="epic-graph"] .floor')?.getBoundingClientRect();
    const card = document.querySelector(`[data-tour="epic-graph"] .svelte-flow__node[data-id="${node}"]`);
    const title = card?.querySelector('.tt');
    if (!floor || !card || !title || card.offsetWidth === 0) return null;
    const r = card.getBoundingClientRect();
    const scale = r.width / card.offsetWidth;
    // What is drawn at the title's centre, the tour's dimming overlay aside
    // (it dims what is not highlighted; it does not hide it): a popover over
    // the title would be found here instead of the card.
    // Hit testing skips `pointer-events: none`, which a tour sets on the card
    // it highlights (`driver-no-interaction`), so the card and everything in
    // it is made hittable for the one read (through CSSOM, which the CSP
    // allows), then put back.
    const t = title.getBoundingClientRect();
    const parts = [card, ...card.querySelectorAll('*')];
    const before = parts.map((el) => [el.style.getPropertyValue('pointer-events'), el.style.getPropertyPriority('pointer-events')]);
    for (const el of parts) el.style.setProperty('pointer-events', 'auto', 'important');
    const hit = document
      .elementsFromPoint(t.left + t.width / 2, t.top + t.height / 2)
      .find((el) => !el.closest('.driver-overlay'));
    parts.forEach((el, i) => {
      const [value, priority] = before[i];
      if (value) el.style.setProperty('pointer-events', value, priority);
      else el.style.removeProperty('pointer-events');
    });
    return {
      covered: hit && card.contains(hit) ? null : hit ? (hit.closest('[role="dialog"]') ?? hit).className?.toString() || hit.tagName : 'nothing',
      box: [r.left, r.top, r.right, r.bottom].map(Math.round),
      floor: [floor.left, floor.top, floor.right, floor.bottom].map(Math.round),
      inside: r.left >= floor.left - 1 && r.top >= floor.top - 1 && r.right <= floor.right + 1 && r.bottom <= floor.bottom + 1,
      px: Math.round(parseFloat(getComputedStyle(title).fontSize) * scale * 10) / 10
    };
  }, id);
  expect(seen !== null, `${at}: no card #${id} with a title on the epic graph`);
  expect(seen.inside, `${at}: card #${id} at ${JSON.stringify(seen.box)} is not inside the node area ${JSON.stringify(seen.floor)}`);
  expect(seen.px >= MIN_CARD_TEXT_PX, `${at}: card #${id}'s title renders at ${seen.px}px, under ${MIN_CARD_TEXT_PX}px`);
  expect(seen.covered === null, `${at}: card #${id}'s title is covered by ${seen.covered}`);
}

/**
 * Fail unless every card of the epic graph is drawn wholly inside the graph's
 * node area (`.dag .floor`): a step that presses Fit all opens on the whole
 * graph, no card cut at the canvas edge (design review c9, D3).
 */
export async function graphFits(page, expect, at) {
  const seen = await page.evaluate(() => {
    const floor = document.querySelector('[data-tour="epic-graph"] .floor')?.getBoundingClientRect();
    const cards = [...document.querySelectorAll('[data-tour="epic-graph"] .svelte-flow__node[data-id]')];
    if (!floor) return { floor: null, cards: cards.length, out: [] };
    const out = cards
      .map((card) => [card.getAttribute('data-id'), card.getBoundingClientRect()])
      .filter(([, r]) => r.left < floor.left - 1 || r.top < floor.top - 1 || r.right > floor.right + 1 || r.bottom > floor.bottom + 1)
      .map(([id, r]) => `${id} at ${JSON.stringify([r.left, r.top, r.right, r.bottom].map(Math.round))}`);
    return { floor: [floor.left, floor.top, floor.right, floor.bottom].map(Math.round), cards: cards.length, out };
  });
  expect(seen.floor !== null && seen.cards > 0, `${at}: no graph node area, or no card (${seen.cards})`);
  expect(seen.out.length === 0, `${at}: cards outside the node area ${JSON.stringify(seen.floor)}: ${seen.out.join(', ')}`);
}

/**
 * A pipeline cell's two caption lines as the page draws them, "label · detail"
 * (`[data-stage]` in `Swimlane.svelte`): the first cell named `stage` in `main`.
 */
export async function stageCaption(page, stage) {
  const cell = page.locator(`main [data-stage="${stage}"]`).first();
  const line = async (which) => {
    const span = cell.locator(`[data-caption="${which}"]`);
    return (await span.count()) === 0 ? '' : squash((await span.first().textContent()) ?? '');
  };
  return [await line('primary'), await line('secondary')].filter((part) => part !== '').join(' · ');
}

/**
 * The run page's layout as a reader meets it (#3459), with every disclosure on
 * it opened first, so what a closed one hides is measured too. Read by
 * `run-page-layout` on the fixture run and by `demo-run-page` on a demo run:
 *
 * - `columns`: each block of the Review-and-route | Cost grid, with its box;
 * - `narrow`: every block (a grid column, a labelled section, a fact list, a
 *   disclosure) under 300px wide;
 * - `cut`: every value drawn shorter than it is — an ellipsis, or a box its
 *   text overflows — with no `title` holding the whole of it;
 * - `figures`: every row of the cost table, `{ figure, value, source }`;
 * - `tokens`: its token rows, and `hostTokens` the host meter's among them;
 * - `order`: the tops of the Safest action and Run events sections;
 * - `sideways`: the page scrolls sideways;
 * - `short`: header and content controls under 32px tall (the event list is
 *   `RunEvents.svelte`'s, checked by its own modules).
 */
export async function runPageLayout(page) {
  // The Cost section, not a hook of the new layout: an old page is measured
  // on what it draws, rather than failing on a selector it never had.
  await page.locator('section[aria-label="Cost"]').waitFor({ state: 'visible', timeout: 15_000 });
  const ids = await page.locator('[data-disclosure]').evaluateAll((els) => els.map((el) => el.id));
  for (const id of ids) {
    const toggle = page.locator(`[data-disclosure="${id}"] > button`);
    if ((await toggle.getAttribute('aria-expanded')) === 'false') await toggle.click();
  }
  await page.waitForFunction(
    () => document.querySelector('[data-disclosure] > button[aria-expanded="false"]') === null,
    undefined,
    { timeout: 5_000 }
  );
  return page.evaluate(() => {
    const squash = (s) => (s ?? '').replace(/\s+/g, ' ').trim();
    const nameOf = (el) =>
      el.getAttribute('aria-label') || el.getAttribute('data-disclosure') || `<${el.tagName.toLowerCase()}>`;
    const box = (el) => el.getBoundingClientRect();
    const content = document.querySelector('[data-run-content]');
    const columns = [...document.querySelectorAll('[data-run-columns] > *')].map((el) => {
      const r = box(el);
      return { name: nameOf(el), left: r.left, top: r.top, width: r.width };
    });
    const blocks = [
      ...document.querySelectorAll(
        '[data-run-columns] > *, [data-run-content] section[aria-label], [data-run-content] dl, [data-disclosure]'
      )
    ];
    const narrow = blocks
      .filter((el) => box(el).width < 300)
      .map((el) => `${nameOf(el)} ${box(el).width.toFixed(0)}px`);
    const values = [
      ...document.querySelectorAll(
        '[data-run-identity] > *, [data-run-content] dd, [data-cost-figure] td, [data-disclosure-summary]'
      )
    ];
    const cut = values
      .filter((el) => {
        const text = squash(el.textContent);
        const style = getComputedStyle(el);
        const overflows = style.display !== 'inline' && el.scrollWidth > el.clientWidth + 1;
        const clipped = overflows && (style.overflowX !== 'visible' || style.textOverflow === 'ellipsis');
        if (!clipped && !text.endsWith('…')) return false;
        const title = squash(el.getAttribute('title'));
        return title === '' || title.endsWith('…') || title.length < text.length;
      })
      .map((el) => `"${squash(el.textContent)}" (title "${el.getAttribute('title') ?? ''}")`);
    const figures = [...document.querySelectorAll('[data-cost-figure]')]
      .map((row) => [...row.querySelectorAll('td')].map((td) => squash(td.textContent)))
      .map(([figure, value, source]) => ({ figure: figure ?? '', value: value ?? '', source: source ?? '' }));
    const tokens = figures.filter(({ figure }) => /tokens/.test(figure));
    // The host meter's four counts, told apart from a runner's "tokens to
    // first edit" by their source, not by their name.
    const hostTokens = tokens.filter(({ source }) => source.startsWith('host-metered'));
    const doc = document.documentElement;
    const sideways =
      doc.scrollWidth > doc.clientWidth + 1 || (content !== null && content.scrollWidth > content.clientWidth + 1);
    const short = [...document.querySelectorAll('header:has([data-run-identity]) a, header:has([data-run-identity]) button, [data-run-content] a, [data-run-content] button')]
      .filter((el) => !el.closest('section[aria-label="Run events"]'))
      .filter((el) => box(el).height > 0 && box(el).height < 32 - 0.5)
      .map((el) => `"${squash(el.textContent)}" ${box(el).height.toFixed(1)}px`);
    const top = (selector) => document.querySelector(selector)?.getBoundingClientRect().top ?? null;
    const order = {
      safest: top('[data-run-content] section[aria-label="Safest action"]'),
      events: top('[data-run-content] section[aria-label="Run events"]')
    };
    return { columns, narrow, cut, figures, tokens, hostTokens, sideways, short, order };
  });
}

/** The arrows fontsource's Plex lacks, which `src/lib/fonts/` carries (#3478). */
export const PLEX_ARROWS = '→↗←';

/**
 * The platform fonts that drew the text of each `[data-plex-probe=…]` element,
 * as the browser reports them through the DevTools protocol's
 * `CSS.getPlatformFontsForNode`, keyed by the attribute's value (null when no
 * element carries it).
 */
export async function drawnFonts(page, keys) {
  const cdp = await page.context().newCDPSession(page);
  try {
    await cdp.send('DOM.enable');
    await cdp.send('CSS.enable');
    const { root } = await cdp.send('DOM.getDocument', { depth: -1 });
    const fonts = {};
    for (const key of keys) {
      const { nodeId } = await cdp.send('DOM.querySelector', {
        nodeId: root.nodeId,
        selector: `[data-plex-probe="${key}"]`
      });
      fonts[key] = nodeId ? (await cdp.send('CSS.getPlatformFontsForNode', { nodeId })).fonts : null;
    }
    return fonts;
  } finally {
    await cdp.detach();
  }
}

/** Why `fonts` are not all IBM Plex web fonts, or null when they are. */
export function notPlex(fonts) {
  if (fonts === null) return 'the probe is not in the page';
  if (fonts.length === 0) return 'the browser reports no font for it';
  const stray = fonts.filter((f) => !f.isCustomFont || !f.familyName.startsWith('IBM Plex'));
  if (stray.length === 0) return null;
  return stray
    .map((f) => `${f.familyName} (${f.isCustomFont ? 'web font' : 'system font'}, ${f.glyphCount} glyphs)`)
    .join(', ');
}

/**
 * Draw {@link PLEX_ARROWS} in `var(--font-mono)` and `var(--font-sans)` at 400,
 * 500 and 600 on `page`, wait for their fonts, and say for each which fonts
 * drew them: `{ name, token, weight, why }`, `why` null when IBM Plex did.
 * Read by `plex-arrows` on the dev server and `demo-plex-arrows` on the demo.
 */
export async function plexArrowProbes(page) {
  const families = [
    { name: 'IBM Plex Mono', token: '--font-mono' },
    { name: 'IBM Plex Sans', token: '--font-sans' }
  ];
  const probes = families.flatMap((family) => [400, 500, 600].map((weight) => ({ ...family, weight })));
  await page.evaluate(
    async ({ probes, arrows }) => {
      for (const { name, token, weight } of probes) {
        const span = document.createElement('span');
        span.dataset.plexProbe = `${token}-${weight}`;
        span.textContent = arrows;
        span.style.fontFamily = `var(${token})`;
        span.style.fontWeight = String(weight);
        span.style.fontStyle = 'normal';
        document.body.append(span);
        await document.fonts.load(`normal ${weight} 16px "${name}"`, arrows);
      }
      await document.fonts.ready;
    },
    { probes, arrows: PLEX_ARROWS }
  );
  const fonts = await drawnFonts(
    page,
    probes.map(({ token, weight }) => `${token}-${weight}`)
  );
  return probes.map((probe) => ({ ...probe, why: notPlex(fonts[`${probe.token}-${probe.weight}`]) }));
}
