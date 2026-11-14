#!/usr/bin/env node
/**
 * `npm run demo-check` — the demo's browser checks against any demo URL (#3207).
 *
 *   npm run build:demo && npm run demo-check      # the local build-demo/
 *   npm run demo-check -- https://demo.kew.dev    # the deployed demo
 *
 * With no URL it serves `build-demo/` on 127.0.0.1 through `static-server.mjs`
 * (the site's CSP, the SPA fallback) and checks that; it never builds it. With
 * one, it checks that base and skips each check that reads the local build
 * from disk, naming it (`LOCAL_BUILD_CHECKS`). Nothing here deploys anything,
 * and no CI job runs it against the live site.
 *
 * Three parts, one browser, one summary:
 *   1. every `ui-checks/demo-*.mjs` module, against the base. One check in
 *      `demo-static.mjs` is about the host, not the build, and is made
 *      against a remote base too: a file under `/_app/immutable/` that no
 *      build writes must answer 404 with no `immutable` in its Cache-Control,
 *      and each script the page loads must answer 200
 *      (`missingAssetProblems`). A host whose fallback covers `/_app/` fails
 *      it, with a sentence that points at `www/DEPLOY.md`, "Caddy site
 *      block";
 *   2. the docs-to-demo check: the docs built as `www/deploy.sh` builds them
 *      (`mkdocs build --strict`, the same pins) into a temp folder, whose
 *      "See it in the demo" links must be exactly the ones the Markdown
 *      sources under `docs/` declare (page, tour and step; `linkSetProblems`),
 *      and every one of them pointed at the base and clicked. At 1440x960 and
 *      1366x650 the tour must open on that step — its title in the popover,
 *      its highlighted element wholly on screen; at 390x844 the page opens
 *      with no tour for as long as a desktop is given to open one, and the
 *      More menu says tours run on a desktop screen. A failure names the docs
 *      page, the tour and the step;
 *   3. the walkthrough gallery: every step of every tour at 1440x960,
 *      1366x650 and 390x844, into `frontend/.shots/demo/<stamp>/` (gitignored)
 *      with a `manifest.json` in `gallery.mjs`'s shape, plus each shot's CSP
 *      violations. A shot that raised anything, a missing screenshot, or a
 *      plan with no shot fails the run, by name.
 *
 * The runner is `ui-check.mjs` (#2570): this script builds the docs, starts
 * the static server when there is no URL, and runs ui-check with the run in
 * its environment (`demoCheckEnv`). ui-check finds the browser, runs the demo
 * modules, then calls `checks()` below with its own `check`, `expect` and
 * browser, and prints the summary — each check that ran, by name.
 */
import { spawn, spawnSync } from 'node:child_process';
import { existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, relative } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import {
  DOCS_PHONE,
  DOCS_SIZES,
  LINK_TEXT,
  LINK_TEXT_ALONE,
  declaredDemoLinks,
  demoCheckEnv,
  demoLinks,
  excludedDocs,
  galleryProblems,
  linkFailure,
  linkSetProblems,
  onBase,
  onSite,
  parseDemoArgs,
  shotPlan,
  shotProblems,
  sizeLabel,
  skippedChecks
} from './demo-check-lib.mjs';
import { DEMO_BUILD_DIR, FRONTEND, SETTLE_MS, TOUR_POPOVER, docsPassage, onScreen, squash, stepSelector } from './ui-check-lib.mjs';
import { TOURS_DESKTOP_ONLY } from '../src/lib/demo/banner.js';
import { serveStatic } from './static-server.mjs';

const REPO = new URL('../', FRONTEND);
const TOURS_DIR = new URL('demo/tours/', FRONTEND);
const DOCS_DIR = new URL('docs/', REPO);
const MORE = 'summary[aria-label="More destinations"]';
/** What the More menu says on a phone (`TourEntry.svelte`, from `banner.js`). */
const TOUR_NOTE = TOURS_DESKTOP_ONLY;

/** How long a docs link's tour is given to open on a desktop, and to stay shut on a phone. */
const TOUR_OPENS_MS = 15_000;

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/** Every Markdown source under `docs/`, as `{ rel, text }` (`declaredDemoLinks`). */
function docsSources(dir = fileURLToPath(DOCS_DIR)) {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) return docsSources(path);
    if (!entry.name.endsWith('.md')) return [];
    return [{ rel: relative(fileURLToPath(DOCS_DIR), path), text: readFileSync(path, 'utf8') }];
  });
}

/** Every tour file, parsed, in name order. */
function tours() {
  return readdirSync(TOURS_DIR)
    .filter((name) => name.endsWith('.json'))
    .sort()
    .map((name) => JSON.parse(readFileSync(new URL(name, TOURS_DIR), 'utf8')));
}

/** Every built page's path under `site`, in name order. */
function htmlPages(site, dir = site) {
  return readdirSync(dir, { withFileTypes: true })
    .flatMap((entry) => {
      const path = join(dir, entry.name);
      if (entry.isDirectory()) return htmlPages(site, path);
      return entry.name.endsWith('.html') ? [relative(site, path)] : [];
    })
    .sort();
}

/** A fresh window at `size`, closed again whatever `body` does. */
async function inWindow(browser, size, body) {
  const context = await browser.newContext({ viewport: size });
  try {
    return await body(await context.newPage());
  } finally {
    await context.close();
  }
}

// --- the docs-to-demo check -------------------------------------------------

/**
 * Open the built docs page, point its `index`-th demo link at `base`, click it,
 * and return the demo page once its `main` is up. The docs are opened from
 * disk: the click is a real one on the page a reader sees.
 *
 * The link is found by its text, as `demoLinks` reads it from the page's
 * source, so the two count the same anchors. Its accessible name is not that
 * text: the docs stylesheet draws "↗" after every link that leaves the docs,
 * and generated content is part of the name.
 */
async function followLink(page, site, rel, index, link, base) {
  await page.goto(pathToFileURL(join(site, rel)).href, { waitUntil: 'load' });
  const anchor = page.locator('a').filter({ hasText: LINK_TEXT_ALONE }).nth(index);
  const href = await anchor.getAttribute('href');
  if (href === null || new URL(href).href !== link.href) {
    throw new Error(`the page's link ${index + 1} reads ${href}, not ${link.href}`);
  }
  const target = onBase(link.href, base);
  await anchor.evaluate((a, to) => {
    a.setAttribute('href', to);
    a.removeAttribute('target');
  }, target);
  await Promise.all([page.waitForURL((url) => onSite(url.href, base), { timeout: 15_000 }), anchor.click()]);
  await page.locator('main').first().waitFor({ state: 'visible', timeout: 15_000 });
}

/** At a desktop size: the popover shows the step's passage and title, over a target on screen. */
async function opensStep(page, step) {
  const failures = [];
  const expect = (ok, message) => {
    if (!ok) throw new Error(message);
  };
  try {
    await page.waitForFunction(
      ([sel, want]) => document.querySelector(sel)?.getAttribute('data-tour-step') === want,
      [TOUR_POPOVER, step.passage],
      { timeout: TOUR_OPENS_MS }
    );
  } catch {
    const shown = await page.locator(TOUR_POPOVER).getAttribute('data-tour-step').catch(() => null);
    throw new Error(shown ? `the popover shows ${shown}` : 'no tour popover opened');
  }
  const want = docsPassage(step.docs, step.passage).title;
  const title = squash((await page.locator(`${TOUR_POPOVER} .driver-popover-title`).textContent()) ?? '');
  if (title !== want) failures.push(`the popover's title is "${title}", the docs say "${want}"`);
  try {
    await onScreen(page, expect, stepSelector(step), 'the highlighted element');
  } catch (error) {
    failures.push(error.message);
  }
  if (failures.length > 0) throw new Error(failures.join('; '));
}

/**
 * On a phone: no tour opens, and the More menu says tours run on a desktop
 * screen. The page gives no signal that it declined the tour, so a phone
 * waits as long as a desktop is given to open one (`TOUR_OPENS_MS`): a tour
 * that opens late on a phone still fails.
 */
async function opensWithoutTour(page) {
  let opened = true;
  try {
    await page.locator(TOUR_POPOVER).first().waitFor({ state: 'attached', timeout: TOUR_OPENS_MS });
  } catch (error) {
    if (error?.name !== 'TimeoutError') throw error;
    opened = false;
  }
  if (opened) throw new Error('a tour opened on a phone');
  // The link no tour can open here is taken out of the address (X6).
  const left = new URL(await page.evaluate(() => location.href));
  if (left.searchParams.has('tour') || left.searchParams.has('step')) throw new Error(`the address kept the tour link: ${left.search}`);
  await page.locator(MORE).click();
  const note = page.locator('[data-tour-note]');
  try {
    await note.waitFor({ state: 'visible', timeout: 5_000 });
  } catch {
    throw new Error('the More menu shows no note about tours');
  }
  const said = squash((await note.textContent()) ?? '');
  if (said !== TOUR_NOTE) throw new Error(`the More menu says "${said}", not "${TOUR_NOTE}"`);
}

async function docsChecks({ browser, base, check, expect, docs }) {
  const byId = new Map(tours().map((tour) => [tour.id, tour]));
  const pages = htmlPages(docs).map((rel) => ({ rel, links: demoLinks(readFileSync(join(docs, rel), 'utf8'), rel) }));
  const links = pages.flatMap(({ rel, links }) => links.map((link, index) => ({ rel, index, link })));
  const declared = declaredDemoLinks(docsSources(), excludedDocs(readFileSync(new URL('mkdocs.yml', REPO), 'utf8')));
  await check(
    `docs to demo: the built docs carry exactly the "${LINK_TEXT}" links the docs sources declare (${declared.length} declared, ${links.length} built)`,
    async () => {
      const problems = linkSetProblems(declared, links.map(({ link }) => link));
      expect(problems.length === 0, problems.join('\n'));
    }
  );
  for (const { rel, index, link } of links) {
    const tour = byId.get(link.tour);
    const step = tour?.steps[link.step - 1];
    const sizes = [...DOCS_SIZES, DOCS_PHONE].map(sizeLabel).join(', ');
    await check(`docs to demo: ${link.page} opens ${link.tour} step ${link.step} (${sizes})`, async () => {
      const failures = [];
      for (const size of [...DOCS_SIZES, DOCS_PHONE]) {
        const fail = (reason) => failures.push(linkFailure(link, tour, size, reason));
        if (!tour) {
          fail('no tour in frontend/demo/tours/ has that id');
          break;
        }
        if (!step) {
          fail(`the tour has ${tour.steps.length} steps`);
          break;
        }
        try {
          await inWindow(browser, size, async (page) => {
            await followLink(page, docs, rel, index, link, base);
            if (size === DOCS_PHONE) await opensWithoutTour(page);
            else await opensStep(page, step);
          });
        } catch (error) {
          fail(String(error.message).split('\n')[0]);
        }
      }
      expect(failures.length === 0, failures.join('\n'));
    });
  }
}

// --- the walkthrough gallery ------------------------------------------------

/** Records every CSP violation the page reports, from before its first script runs. */
function recordCsp() {
  window.__kewCsp = [];
  document.addEventListener('securitypolicyviolation', (event) => {
    window.__kewCsp.push(`${event.violatedDirective} ${event.blockedURI || '(inline)'}`);
  });
}

/** One shot: open the step's deep link at its size, wait for its popover on a desktop, photograph it. */
async function shoot(browser, base, shots, shot) {
  const errors = [];
  const entry = { name: shot.name, route: shot.route, width: shot.width, height: shot.height, file: shot.file, errors };
  await inWindow(browser, { width: shot.width, height: shot.height }, async (page) => {
    await page.addInitScript(recordCsp);
    page.on('pageerror', (error) => errors.push(`page error: ${error.message}`));
    page.on('console', (message) => {
      if (message.type() === 'error') errors.push(`console error: ${message.text()}`);
    });
    try {
      await page.goto(`${base}${shot.route}`, { waitUntil: 'load', timeout: 20_000 });
      await page.locator('main').first().waitFor({ state: 'visible', timeout: 15_000 });
      if (shot.width !== DOCS_PHONE.width) {
        try {
          await page.waitForFunction(
            ([sel, want]) => document.querySelector(sel)?.getAttribute('data-tour-step') === want,
            [TOUR_POPOVER, shot.passage],
            { timeout: 15_000 }
          );
        } catch {
          errors.push(`tour: the popover never showed ${shot.passage}`);
        }
      }
      await sleep(SETTLE_MS);
      entry.pageScrollsSideways = await page.evaluate(
        () => document.documentElement.scrollWidth > document.documentElement.clientWidth
      );
      entry.csp = await page.evaluate(() => window.__kewCsp ?? []);
      await page.screenshot({ path: join(shots, shot.file) });
    } catch (error) {
      errors.push(`load: ${String(error.message).split('\n')[0]}`);
    }
  });
  entry.csp ??= [];
  entry.pageScrollsSideways ??= false;
  return entry;
}

async function galleryChecks({ browser, base, check, expect, shots }) {
  const plan = shotPlan(tours());
  await check(`gallery: every tour step at ${[...DOCS_SIZES, DOCS_PHONE].map(sizeLabel).join(', ')}, with no error`, async () => {
    mkdirSync(shots, { recursive: true });
    const manifest = { base, mode: 'demo', taken: new Date().toISOString(), skipped: [], shots: [] };
    for (const shot of plan) manifest.shots.push(await shoot(browser, base, shots, shot));
    writeFileSync(join(shots, 'manifest.json'), `${JSON.stringify(manifest, null, 2)}\n`);
    console.log(`        ${manifest.shots.length} shots -> ${shots}`);
    const raised = [
      ...galleryProblems(plan, manifest.shots, (file) => existsSync(join(shots, file))),
      ...manifest.shots.flatMap((shot) => shotProblems(shot).map((problem) => `${shot.file}: ${problem}`))
    ];
    expect(raised.length === 0, `the gallery's shots raised:\n${raised.slice(0, 20).join('\n')}`);
  });
}

/** Called by `ui-check.mjs` in demo-check mode, after the demo modules, with its runner's args. */
export async function checks(args) {
  await docsChecks(args);
  await galleryChecks(args);
}

// --- run --------------------------------------------------------------------

/** The docs, built with `www/deploy.sh`'s pins, into a temp folder it returns. */
function buildDocs() {
  const site = mkdtempSync(join(tmpdir(), 'kew-demo-check-docs-'));
  const build = spawnSync(
    'uvx',
    ['--from', 'mkdocs>=1.6,<2', '--with', 'mkdocs-material>=9,<10', 'mkdocs', 'build', '--strict', '--config-file', 'mkdocs.yml', '-d', site],
    { cwd: fileURLToPath(REPO), encoding: 'utf8', maxBuffer: 64 * 1024 * 1024 }
  );
  if (build.status !== 0) {
    rmSync(site, { recursive: true, force: true });
    throw new Error(`mkdocs build --strict exited ${build.status ?? build.error?.message ?? build.signal}\n${build.stdout ?? ''}${build.stderr ?? ''}`);
  }
  return site;
}

/** `ui-check.mjs` in demo-check mode, to completion; resolves to its exit code. */
function runUiCheck(env) {
  return new Promise((resolve) => {
    const child = spawn(process.execPath, [fileURLToPath(new URL('ui-check.mjs', import.meta.url))], {
      cwd: fileURLToPath(FRONTEND),
      env: { ...process.env, ...env },
      stdio: 'inherit'
    });
    child.on('exit', (code, signal) => resolve(code ?? (signal ? 1 : 0)));
  });
}

async function main() {
  let run;
  try {
    run = parseDemoArgs(process.argv.slice(2));
  } catch (error) {
    console.error(`demo-check: ${error.message}\nusage: npm run demo-check -- [<base-url>]`);
    return 2;
  }
  if (!run.remote && !existsSync(fileURLToPath(new URL('index.html', DEMO_BUILD_DIR)))) {
    console.error('demo-check: build-demo/ has no index.html — run `npm run build:demo` first');
    return 1;
  }
  for (const skipped of skippedChecks(run)) {
    console.log(`demo-check: a remote base skips "${skipped.name}": ${skipped.why}`);
  }
  console.log('demo-check: mkdocs build --strict');
  let docs;
  try {
    docs = buildDocs();
  } catch (error) {
    console.error(`demo-check: ${error.message}`);
    return 1;
  }
  const stamp = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
  const shots = fileURLToPath(new URL(`.shots/demo/${stamp}/`, FRONTEND));
  const server = run.remote ? null : await serveStatic(DEMO_BUILD_DIR);
  try {
    const base = run.base ?? server.base;
    console.log(`demo-check: ${run.remote ? 'the demo at' : 'build-demo/ on'} ${base}`);
    return await runUiCheck(demoCheckEnv({ base, remote: run.remote, docs, shots }));
  } finally {
    if (server) await server.close();
    rmSync(docs, { recursive: true, force: true });
  }
}

// Run only as a script: ui-check imports `checks()` from here.
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) process.exit(await main());
