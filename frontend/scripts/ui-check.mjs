#!/usr/bin/env node
/**
 * `npm run ui-check` — the browser checks a vitest run cannot make (#2273).
 *
 *   npm run ui-check
 *   npm run ui-check -- work-rows epic-rail   # only those surface modules (#2984)
 *   npm run ui-check -- --builds work-rows    # those modules, and both builds (#3213)
 *   KEW_UI_CHECK_CHROMIUM=/path/to/chrome npm run ui-check
 *
 * Four UI changes passed vitest and an adversarial review and were still broken
 * on screen: the epic canvas rendered at 0 height, the Monitor presets ignored
 * the URL, graph edges took the clicks meant for nodes, and a Work row click
 * did not open the item. None of those is reachable from jsdom — they are
 * layout, a real wheel event, hit testing and client routing. This drives a
 * real Chromium over the fixture-mode dev server and asserts them.
 *
 * It starts `vite dev` itself with `KEW_UI_FIXTURES=1` on a free port, so the
 * readings are the committed fixtures and no `kew serve` has to be running. The
 * server is always stopped again, including on a failure.
 *
 * NO BROWSER IS DOWNLOADED. `playwright-core` is the driver only; the Chromium
 * is one the machine already has:
 *   1. `$KEW_UI_CHECK_CHROMIUM`, if set (and it must then exist);
 *   2. `$PLAYWRIGHT_BROWSERS_PATH`, if set — the shared directory a
 *      provisioned verify host installs its headless shell into
 *      (`/opt/playwright-browsers`, kew/src/kew/hosts/provision/);
 *   3. the Playwright cache — `~/Library/Caches/ms-playwright` on macOS,
 *      `~/.cache/ms-playwright` on Linux (`npx playwright install
 *      chromium-headless-shell` puts one there).
 * With none, this prints `skipped: no Chromium` and exits 0: CI without a
 * browser must not go red over a check it cannot make. A merge verify is not
 * that: it sets `KEW_UI_CHECK_REQUIRE_CHROMIUM=1`, and then no browser is a
 * failure, exit 1, never a skip (#2630). A re-provisioned verify host once had
 * its browser in a directory only a login shell's profile named, and the job's
 * shell sources no profile — so the gate "passed" without a check being made.
 *
 * THE CHECKS ARE NOT IN THIS FILE (#2570). This is the runner: the browser, the
 * fixture dev server, `check()`, `visit()`, `expect()` and the summary. Each
 * surface's checks live in their own module, `scripts/ui-checks/<name>.mjs`,
 * exporting
 *
 *   export async function checks({ browser, base, context, check, visit, expect, sleep, sizes })
 *
 * and the runner imports every module in that folder in name order
 * (`runChecks` in `ui-check-lib.mjs`). A new surface adds a new module; it never
 * edits this file or a sibling, so two UI changes no longer conflict here.
 * What more than one module reads lives in `scripts/ui-check-lib.mjs`.
 *
 * TWO PHASES (#3139). A module named `demo-*.mjs` checks the static demo build
 * (`npm run build:demo`, a backend-free console on bundled fixtures) and runs
 * only against a standard-library static server for `build-demo/`
 * (`static-server.mjs`, which sends the site's CSP); every other module runs
 * only against the dev server. The builds happen FIRST, before the dev server
 * or the browser starts, never alongside them: the 300 ms timing checks flake
 * under a build's load. The default run builds both — `npm run build`, whose
 * output it then greps for anything demo- or fixture-only (`buildLeaks`), and
 * `npm run build:demo`; a scoped run builds the demo only when it names a
 * `demo-*` module, and never runs `npm run build` — unless it passes
 * `--builds`, as kew's scoped merge verify does, which makes both.
 *
 * DEMO-CHECK MODE (#3207). `npm run demo-check -- [<base-url>]`
 * (`demo-check.mjs`) runs this runner with `KEW_DEMO_CHECK_BASE` set
 * (`demoCheckEnv`): no build, no dev server and no static server — the
 * `demo-*` modules run against that base, with `remote` passed to each
 * module's `checks()` and a `skip(name, why)` that prints a check a remote base
 * cannot make. Then `demo-check.mjs`'s own checks run in the same browser:
 * every docs "See it in the demo" link, and the walkthrough gallery.
 */
import { spawn, spawnSync } from 'node:child_process';
import { existsSync, readdirSync } from 'node:fs';
import { createServer } from 'node:net';
import { homedir, platform } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright-core';
import {
  BUILD_DIR,
  DEMO_BUILD_DIR,
  FRONTEND,
  HEIGHT,
  SETTLE_MS,
  WIDTH,
  buildLeaks,
  buildsToRun,
  checkModules,
  checkRecorder,
  checksDirFrom,
  chromiumRequired,
  exitGuard,
  inPhase,
  npmEnv,
  runChecks,
  runScope,
  scopedModules,
  settleThrownRoutes
} from './ui-check-lib.mjs';
import { serveStatic } from './static-server.mjs';
import { demoCheckTarget } from './demo-check-lib.mjs';

const VITE = fileURLToPath(new URL('node_modules/vite/bin/vite.js', FRONTEND));

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// --- the browser -----------------------------------------------------------

const CACHE_DIR =
  platform() === 'darwin'
    ? join(homedir(), 'Library', 'Caches', 'ms-playwright')
    : join(homedir(), '.cache', 'ms-playwright');

/** Where to look, in order: the directory `$PLAYWRIGHT_BROWSERS_PATH` names,
 *  then this user's cache. `0` is Playwright's "inside node_modules", which
 *  playwright-core alone never installs into, so it names nothing here. */
function browserDirs() {
  const named = process.env.PLAYWRIGHT_BROWSERS_PATH;
  return named && named !== '0' ? [named, CACHE_DIR] : [CACHE_DIR];
}

/** The binary name differs by Playwright version, build and platform; the
 *  enclosing directory layout differs even more. Match on the name. */
const BINARY_NAMES = new Set(['chrome-headless-shell', 'headless_shell', 'chrome', 'Chromium']);

/** Depth 5 reaches both `chrome-headless-shell-linux64/chrome-headless-shell`
 *  and the macOS bundle's `chrome-mac/Chromium.app/Contents/MacOS/Chromium`. */
function findBinary(dir, depth = 0) {
  let entries;
  try {
    entries = readdirSync(dir, { withFileTypes: true });
  } catch {
    return null;
  }
  for (const entry of entries) {
    if (entry.isFile() && BINARY_NAMES.has(entry.name)) return join(dir, entry.name);
  }
  if (depth >= 5) return null;
  for (const entry of entries) {
    if (!entry.isDirectory()) continue;
    const found = findBinary(join(dir, entry.name), depth + 1);
    if (found) return found;
  }
  return null;
}

/** The headless shell first (it is what a CI machine caches), then a full
 *  Chromium, newest revision first within each kind. */
function cachedChromium() {
  for (const dir of browserDirs()) {
    const binary = chromiumIn(dir);
    if (binary) return binary;
  }
  return null;
}

function chromiumIn(dir) {
  let names;
  try {
    names = readdirSync(dir);
  } catch {
    return null;
  }
  const found = names
    .map((name) => /^(chromium_headless_shell|chromium)-(\d+)$/.exec(name))
    .filter((match) => match !== null)
    .sort((a, b) =>
      a[1] === b[1]
        ? Number(b[2]) - Number(a[2])
        : a[1] === 'chromium_headless_shell'
          ? -1
          : 1
    );
  for (const match of found) {
    const binary = findBinary(join(dir, match[0]));
    if (binary) return binary;
  }
  return null;
}

/** `null` means "no browser here", which is a skip unless the run requires
 *  one. A *named* browser that does not exist is a mistake, not a skip, and
 *  says so. */
function chromiumBinary() {
  const named = process.env.KEW_UI_CHECK_CHROMIUM;
  if (named) {
    if (!existsSync(named)) {
      console.error(`ui-check: $KEW_UI_CHECK_CHROMIUM is set to ${named}, which does not exist`);
      process.exit(1);
    }
    return named;
  }
  return cachedChromium();
}

// --- the dev server --------------------------------------------------------

function freePort() {
  return new Promise((resolve, reject) => {
    const probe = createServer();
    probe.on('error', reject);
    probe.listen(0, '127.0.0.1', () => {
      const { port } = probe.address();
      probe.close(() => resolve(port));
    });
  });
}

function startDevServer(port) {
  if (!existsSync(VITE)) {
    console.error('ui-check: no node_modules/vite — run `npm install` in frontend/ first');
    process.exit(1);
  }
  const child = spawn(
    process.execPath,
    [VITE, 'dev', '--port', String(port), '--strictPort', '--host', '127.0.0.1'],
    {
      cwd: fileURLToPath(FRONTEND),
      env: { ...process.env, KEW_UI_FIXTURES: '1' },
      stdio: ['ignore', 'pipe', 'pipe']
    }
  );
  // A run that ends without reaching its `finally` (a module's own
  // `process.exit`) must not leave the server running.
  process.on('exit', () => {
    if (child.exitCode === null && child.signalCode === null) child.kill('SIGKILL');
  });
  const log = [];
  const keep = (chunk) => log.push(String(chunk));
  child.stdout.on('data', keep);
  child.stderr.on('data', keep);
  return { child, log };
}

async function waitForServer(base, child, log, timeoutMs = 90_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (child.exitCode !== null) {
      throw new Error(`vite dev exited ${child.exitCode}\n${log.join('')}`);
    }
    try {
      const response = await fetch(`${base}/work`);
      if (response.status < 500) return;
    } catch {
      // Not listening yet.
    }
    await sleep(250);
  }
  throw new Error(`vite dev never answered on ${base}\n${log.join('')}`);
}

async function stopDevServer(child) {
  if (child.exitCode !== null || child.signalCode !== null) return;
  child.kill('SIGTERM');
  const deadline = Date.now() + 5_000;
  while (child.exitCode === null && child.signalCode === null && Date.now() < deadline) {
    await sleep(100);
  }
  if (child.exitCode === null && child.signalCode === null) child.kill('SIGKILL');
}

// --- the checks ------------------------------------------------------------

// `check()`, `skip()` and the results the summary counts are `checkRecorder`'s,
// made before the builds run: a route handler that throws fails its own check,
// never the run, and a check that never settles fails at its deadline (#3477).

function expect(ok, message) {
  if (!ok) throw new Error(message);
}

async function main(browser, base, phase, target = null) {
  const context = await browser.newContext({ viewport: { width: WIDTH, height: HEIGHT } });
  // A route handler that throws aborts its route, on every context and page
  // (#3477): a page awaiting that request must not wait forever.
  settleThrownRoutes(context);
  const probe = await context.newPage();
  settleThrownRoutes(probe);
  await probe.close();

  /** One page per check, with its own error log: a route that shouts must not
   *  fail the check after it. `size` gives the check its own window — the
   *  phone and short-window readings are about a viewport, and a context is
   *  the only place Playwright lets one be set. `size.scale`, if given, is
   *  the window's device pixel ratio, for a check that reads sub-pixel
   *  paint off a screenshot. */
  /** `prepare` runs on the page before it navigates — where a check installs
   * the `page.route` that serves it a fixture the committed set lacks. */
  async function visit(route, body, size, prepare) {
    const { scale, ...viewport } = size ?? {};
    const viewContext = size
      ? await browser.newContext({ viewport, ...(scale ? { deviceScaleFactor: scale } : {}) })
      : context;
    const page = await viewContext.newPage();
    if (prepare) await prepare(page);
    const problems = [];
    page.on('pageerror', (error) => problems.push(`page error: ${error.message}`));
    page.on('console', (message) => {
      if (message.type() === 'error') problems.push(`console error: ${message.text()}`);
    });
    try {
      await page.goto(`${base}${route}`, { waitUntil: 'load' });
      await page.locator('main').first().waitFor({ state: 'visible', timeout: 15_000 });
      await sleep(SETTLE_MS);
      return await body(page, problems);
    } finally {
      await page.close();
      if (size) await viewContext.close();
    }
  }

  const remote = target?.remote ?? false;
  await runChecks({ browser, base, context, check, visit, expect, sleep, sizes: { WIDTH, HEIGHT }, remote, skip }, checksDir, only, phase);
  if (target) {
    const demoCheck = await import('./demo-check.mjs');
    await demoCheck.checks({ browser, base, check, expect, docs: target.docs, shots: target.shots });
  }

  await context.close();
}

// --- the builds ------------------------------------------------------------

/** `npm run <script>` in frontend/, to completion. Its output is printed only
 *  when it fails: a passing build is noise in a check log. */
function npmRun(script) {
  const run = spawnSync(process.platform === 'win32' ? 'npm.cmd' : 'npm', ['run', script], {
    cwd: fileURLToPath(FRONTEND),
    env: npmEnv(script),
    encoding: 'utf8',
    maxBuffer: 64 * 1024 * 1024
  });
  if (run.status === 0) return;
  throw new Error(`npm run ${script} exited ${run.status ?? run.signal}\n${run.stdout}${run.stderr}`);
}

const BUILD_CLEAN = 'the normal build carries no demo code, no tour code and no fixture data';
const DEMO_BUILT = 'npm run build:demo writes the static demo console';

// --- run -------------------------------------------------------------------

// A scoped run names its modules; a name with no module fails here, before a
// browser or a dev server is started for it.
const target = demoCheckTarget();
const scope = target ? { only: [], builds: false } : runScope(process.argv.slice(2));
const { only } = scope;
const checksDir = checksDirFrom();
let selected;
try {
  selected = target ? inPhase(checkModules(checksDir), 'demo') : scopedModules(checkModules(checksDir), scope, checksDir);
} catch (error) {
  console.error(`ui-check: ${error.message}`);
  process.exit(1);
}
if (only.length > 0) console.log(`ui-check: only ${only.join(', ')}`);
if (scope.builds) console.log('ui-check: --builds: both builds run, as in a full run');
if (target) console.log(`ui-check: demo-check against ${target.base}${target.remote ? ' (remote)' : ''}`);
// A demo-check checks a demo that is already built and served: it builds nothing.
const builds = target ? { build: false, demo: false } : buildsToRun(scope);

const binary = chromiumBinary();
if (!binary && chromiumRequired(only)) {
  const why = only.length > 0 ? 'a scoped run names modules a browser must load' : 'KEW_UI_CHECK_REQUIRE_CHROMIUM=1';
  console.error(
    `ui-check: no Chromium, and this run requires one (${why}). ` +
      `Looked in: ${browserDirs().join(', ')}. Set $KEW_UI_CHECK_CHROMIUM or ` +
      '$PLAYWRIGHT_BROWSERS_PATH to where the headless shell is.'
  );
  process.exit(1);
}
if (!binary) {
  console.log(
    'ui-check: skipped: no Chromium. Set $KEW_UI_CHECK_CHROMIUM, or run ' +
      '`npx playwright install chromium-headless-shell` to cache one.'
  );
  process.exit(0);
}
console.log(`ui-check: ${binary}`);

// Chromium refuses its own sandbox as root (CI images, dev containers). Only
// then, and never on an ordinary account.
const launchArgs = typeof process.getuid === 'function' && process.getuid() === 0 ? ['--no-sandbox'] : [];

// A browser that cannot start fails the run now, not after the builds have
// spent minutes. It is closed again at once: the builds run with no browser
// and no server up (see the header).
try {
  const probe = await chromium.launch({ executablePath: binary, args: launchArgs });
  await probe.close();
} catch (error) {
  console.log('  FAIL  ui-check could not run');
  for (const line of String(error.stack ?? error.message).split('\n')) console.log(`        ${line}`);
  console.log('ui-check: 0/1 checks passed');
  process.exit(1);
}

// From here only the summary may end the run with exit 0 (#3477).
const guard = exitGuard();

// The builds, before any server or browser starts (see the header).
const { results, skipped, skip, check } = checkRecorder();
if (builds.build) {
  console.log('ui-check: npm run build');
  await check(BUILD_CLEAN, async () => {
    npmRun('build');
    const leaks = buildLeaks(BUILD_DIR);
    expect(leaks.length === 0, `build/ holds what only the demo or the fixtures may:\n${leaks.join('\n')}`);
  });
}
let demoReady = false;
if (builds.demo) {
  console.log('ui-check: npm run build:demo');
  await check(DEMO_BUILT, async () => {
    npmRun('build:demo');
    expect(existsSync(fileURLToPath(new URL('index.html', DEMO_BUILD_DIR))), 'build-demo/ has no index.html');
    demoReady = true;
  });
}

const devModules = inPhase(selected, 'dev');
const demoModules = inPhase(selected, 'demo');

let browser = null;
let child = null;
let demoServer = null;
try {
  if (devModules.length > 0) {
    const port = await freePort();
    const base = `http://127.0.0.1:${port}`;
    console.log(`ui-check: vite dev (KEW_UI_FIXTURES=1) on ${base}`);
    const started = startDevServer(port);
    child = started.child;
    await waitForServer(base, child, started.log);
    browser = await chromium.launch({ executablePath: binary, args: launchArgs });
    await main(browser, base, 'dev');
    await stopDevServer(child);
  }
  if (target) {
    browser = await chromium.launch({ executablePath: binary, args: launchArgs });
    await main(browser, target.base, 'demo', target);
  } else if (demoModules.length > 0 && demoReady) {
    demoServer = await serveStatic(DEMO_BUILD_DIR);
    console.log(`ui-check: static demo build on ${demoServer.base}`);
    browser ??= await chromium.launch({ executablePath: binary, args: launchArgs });
    await main(browser, demoServer.base, 'demo');
  }
} catch (error) {
  console.log('  FAIL  ui-check could not run');
  for (const line of String(error.stack ?? error.message).split('\n')) console.log(`        ${line}`);
  results.push({ name: 'ui-check could not run', ok: false });
} finally {
  if (browser) await browser.close().catch(() => {});
  if (child) await stopDevServer(child);
  if (demoServer) await demoServer.close();
}

// A scoped run that made no check proved nothing about the surfaces it named.
if (only.length > 0 && results.length === 0) {
  console.log('  FAIL  the named modules made no browser check');
  results.push({ name: 'scoped run made no check', ok: false });
}

const failed = results.filter((result) => !result.ok);
const skips = skipped.length > 0 ? `, ${skipped.length} skipped` : '';
console.log(`ui-check: ${results.length - failed.length}/${results.length} checks passed${skips}`);
guard.done();
process.exit(failed.length === 0 ? 0 : 1);
