#!/usr/bin/env node
// Record the web console demo for the README: docs/img/demo-console.gif.
//   cd frontend && npm run build:demo && node scripts/record-demo.mjs [--out <file.gif>]
// It serves build-demo/ (the public demo world only), drives one take, and
// converts it with ffmpeg ($FFMPEG, else on PATH). Chromium: $KEW_UI_CHECK_CHROMIUM,
// else the newest headless shell in the Playwright cache. It never builds the demo.
/**
 * What it does, in order: serve `build-demo/` through `static-server.mjs` (the
 * site's CSP, the SPA fallback), open it at 1280x874 in the dark theme, and
 * take one continuous walk through the console — Work, the ledger-v2 epic's
 * graph, a merged node's proof, the refused merge on #107, back to Work.
 *
 * Frames come from Chromium's own screencast (`Page.startScreencast`, PNG),
 * each with its timestamp, and are resampled to a constant frame rate before
 * the two-pass palette conversion. Playwright's `recordVideo` was not used: it
 * encodes VP8 at a fixed 1 Mbit/s, which smears 11-13 px text.
 *
 * A headless browser draws no pointer, so the page gets a drawn one: an arrow
 * that follows the real mouse events Playwright dispatches. The pointer always
 * glides; it never jumps.
 *
 * The screencast skips a paint while it waits for its previous frame to be
 * acknowledged, and a still page paints nothing more, so a skipped paint would
 * leave the frame before it on screen for the whole hold. Every settle
 * therefore forces one fresh frame (`refresh`) before the hold starts.
 *
 * It fails (exit 1, no GIF written) when the demo build is missing, a selector
 * is not found, the page logs an error, a request fails, the root error page
 * shows, the page scrolls sideways, a line of text is cut through by the top or
 * bottom of its pane or by the fixed footer (graph cards excepted: the canvas
 * pans them past its edge; text cut at a pane's side is not checked), or
 * the GIF is over the 5 MB cap. Ctrl-C cleans up the same way and exits 130.
 */
import { spawnSync } from 'node:child_process';
import { copyFileSync, existsSync, mkdirSync, mkdtempSync, readdirSync, rmSync, statSync, writeFileSync } from 'node:fs';
import { homedir, platform, tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { chromium } from 'playwright-core';
import { DEMO_BUILD_DIR, FRONTEND } from './ui-check-lib.mjs';
import { serveStatic } from './static-server.mjs';

/**
 * 874 px tall: of the heights tried from 800 to 1000, only 874 and 876 put the
 * frame's bottom edge (the fixed footer's top) between text rows on every
 * visited view, and `settled` checks that again on each take.
 * At 800 it cut the epic's SELECTED panel and #107's merge-verify heading; at
 * 900, the panel's "Open PR #207 to merge"; at 880, a Work list row.
 */
const VIEWPORT = { width: 1280, height: 874 };
/** The GIF: width in pixels, frames per second, size cap in bytes. */
const GIF_WIDTH = 1000;
const GIF_FPS = 12;
const MAX_BYTES = 5 * 1024 * 1024;
/** Chosen by eye at 1000 px: as clean as `bayer:bayer_scale=5` on text and the ink floor, and about 5% smaller. */
const DITHER = 'sierra2_4a';
/** No action follows a navigation sooner than this (the storyboard's floor). */
const AFTER_NAV_MS = 1200;
const FIND_MS = 10_000;
const DEFAULT_OUT = fileURLToPath(new URL('../docs/img/demo-console.gif', FRONTEND));

const sleep = (ms) => new Promise((done) => setTimeout(done, ms));

/** Every failure throws this; the run cleans up, prints it and exits 1, writing no GIF. */
class RecordError extends Error {}

function die(message) {
  throw new RecordError(message);
}

function parseArgs(argv) {
  if (argv.length === 0) return { out: DEFAULT_OUT };
  if (argv.length === 2 && argv[0] === '--out' && argv[1].endsWith('.gif')) return { out: resolve(argv[1]) };
  return die('usage: node scripts/record-demo.mjs [--out <file.gif>]');
}

/** `$KEW_UI_CHECK_CHROMIUM`, else the newest cached headless shell (as `ui-check.mjs` and `gallery.mjs` find it). */
function chromiumBinary() {
  const named = process.env.KEW_UI_CHECK_CHROMIUM;
  if (named) return existsSync(named) ? named : die(`$KEW_UI_CHECK_CHROMIUM is set to ${named}, which does not exist`);
  const cache = process.env.PLAYWRIGHT_BROWSERS_PATH && process.env.PLAYWRIGHT_BROWSERS_PATH !== '0'
    ? process.env.PLAYWRIGHT_BROWSERS_PATH
    : join(homedir(), platform() === 'darwin' ? 'Library/Caches/ms-playwright' : '.cache/ms-playwright');
  const shells = existsSync(cache)
    ? readdirSync(cache)
        .filter((name) => /^chromium_headless_shell-\d+$/.test(name))
        .sort((a, b) => Number(b.split('-')[1]) - Number(a.split('-')[1]))
    : [];
  for (const shell of shells) {
    for (const sub of ['chrome-headless-shell-mac-arm64', 'chrome-headless-shell-mac-x64', 'chrome-headless-shell-linux64']) {
      const bin = join(cache, shell, sub, 'chrome-headless-shell');
      if (existsSync(bin)) return bin;
    }
  }
  return die('no Chromium: set $KEW_UI_CHECK_CHROMIUM, or run `npx playwright install chromium-headless-shell`');
}

function ffmpegBinary() {
  const bin = process.env.FFMPEG || 'ffmpeg';
  const probe = spawnSync(bin, ['-version'], { encoding: 'utf8' });
  return probe.status === 0 ? bin : die(`no ffmpeg at "${bin}": install it or set $FFMPEG`);
}

/** The drawn pointer: an arrow that follows mouse events, pressed in on mousedown. Installed before any page script. */
function drawPointer() {
  const install = () => {
    if (document.getElementById('kew-record-pointer')) return;
    const pointer = document.createElement('div');
    pointer.id = 'kew-record-pointer';
    pointer.innerHTML =
      '<svg width="22" height="26" viewBox="0 0 22 26" aria-hidden="true">' +
      '<path d="M3 2 L3 20 L7.6 16 L10.8 23.4 L14 22 L10.9 14.8 L17.2 14.8 Z" ' +
      'fill="#ebe6f2" stroke="#0a0714" stroke-width="1.6" stroke-linejoin="round"/></svg>';
    Object.assign(pointer.style, {
      position: 'fixed',
      left: '0',
      top: '0',
      zIndex: '2147483647',
      pointerEvents: 'none',
      transform: 'translate(-200px, -200px)',
      transformOrigin: '3px 2px'
    });
    document.documentElement.appendChild(pointer);
    const at = { x: -200, y: -200, scale: 1 };
    // The arrow's tip (3, 2) sits on the pointer's position.
    const place = (change) => {
      Object.assign(at, change);
      pointer.style.transform = `translate(${at.x - 3}px, ${at.y - 2}px) scale(${at.scale})`;
    };
    document.addEventListener('mousemove', (event) => place({ x: event.clientX, y: event.clientY }), true);
    document.addEventListener('mousedown', () => place({ scale: 0.86 }), true);
    document.addEventListener('mouseup', () => place({ scale: 1 }), true);
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', install);
  else install();
}

/** Everything the page reports that a recording must not carry. */
function watchForErrors(page, base) {
  const errors = [];
  page.on('pageerror', (error) => errors.push(`page error: ${error.message}`));
  page.on('console', (message) => {
    if (message.type() === 'error') errors.push(`console error: ${message.text()}`);
  });
  page.on('requestfailed', (request) => errors.push(`request failed: ${request.url()} (${request.failure()?.errorText})`));
  page.on('response', (response) => {
    if (response.status() >= 400) errors.push(`HTTP ${response.status()}: ${response.url()}`);
  });
  page.on('request', (request) => {
    const url = request.url();
    if (!url.startsWith(base) && !url.startsWith('data:')) errors.push(`left the demo build: ${url}`);
  });
  return errors;
}

/** The screencast: every frame Chromium paints, as a PNG with its timestamp (seconds). */
async function startFrames(page, dir) {
  const cdp = await page.context().newCDPSession(page);
  const frames = [];
  cdp.on('Page.screencastFrame', ({ data, metadata, sessionId }) => {
    const file = join(dir, `f${String(frames.length).padStart(5, '0')}.png`);
    writeFileSync(file, Buffer.from(data, 'base64'));
    frames.push({ file, ts: metadata.timestamp });
    cdp.send('Page.screencastFrameAck', { sessionId }).catch(() => {});
  });
  await cdp.send('Page.startScreencast', { format: 'png', maxWidth: VIEWPORT.width, maxHeight: VIEWPORT.height, everyNthFrame: 1 });
  const mark = () => Date.now() / 1000;
  return {
    frames,
    /** Now, on the screencast's clock: a frame painted after this mark carries a later timestamp. */
    mark,
    /** Force one fresh frame of the page as it is now: an invisible change to the drawn pointer repaints it. */
    async refresh() {
      const since = mark();
      await page.evaluate(() => {
        const drawn = document.getElementById('kew-record-pointer');
        if (drawn) drawn.style.opacity = drawn.style.opacity === '0.999' ? '1' : '0.999';
      });
      const deadline = Date.now() + 3000;
      while (!(frames.at(-1)?.ts > since)) {
        if (Date.now() > deadline) die('the screencast sent no frame after a settle');
        await sleep(15);
      }
    },
    stop: () => cdp.send('Page.stopScreencast')
  };
}

/**
 * Every line of text the frame shows cut through: a line box that a clipping
 * ancestor (overflow other than visible) or the fixed footer's top edge splits.
 * Graph cards and the minimap are left out: the canvas pans them past its edge.
 */
function cutLines() {
  const footer = document.querySelector('[data-tour="build-history"]');
  // Without the footer the check cannot see where the frame's text area ends.
  if (!footer) return ['the build-history footer is not on the page, so its edge cannot be checked'];
  const footTop = footer.getBoundingClientRect().top;
  const cut = [];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const el = node.parentElement;
    if (!node.textContent.trim() || !el || el.closest('.svelte-flow__node, .svelte-flow__minimap')) continue;
    const style = getComputedStyle(el);
    if (style.visibility === 'hidden' || Number(style.opacity) === 0) continue;
    const range = document.createRange();
    range.selectNodeContents(node);
    for (const line of range.getClientRects()) {
      if (line.height < 4 || line.width < 2) continue;
      let top = 0;
      let bottom = footer?.contains(el) ? innerHeight : Math.min(innerHeight, footTop);
      for (let a = el; a && a !== document.documentElement; a = a.parentElement) {
        if (!/(hidden|auto|scroll|clip)/.test(getComputedStyle(a).overflowY)) continue;
        const box = a.getBoundingClientRect();
        top = Math.max(top, box.top);
        bottom = Math.min(bottom, box.bottom);
      }
      const shown = Math.min(line.bottom, bottom) - Math.max(line.top, top);
      if (shown > 1 && shown < line.height - 1) cut.push(`"${node.textContent.trim().slice(0, 40)}" at y ${Math.round(line.top)}`);
    }
  }
  return cut;
}

/** The walk's pointer: glides with an ease over `ms` of wall time, never a jump. */
function pointer(page) {
  const at = { x: 0, y: 0 };
  const glide = async (x, y, ms) => {
    const from = { ...at };
    const began = Date.now();
    for (let t = 0; t < 1; ) {
      await sleep(12);
      t = Math.min(1, (Date.now() - began) / ms);
      const e = t < 0.5 ? 2 * t * t : 1 - (-2 * t + 2) ** 2 / 2;
      await page.mouse.move(from.x + (x - from.x) * e, from.y + (y - from.y) * e);
    }
    Object.assign(at, { x, y });
  };
  const centre = async (locator, what) => {
    try {
      await locator.waitFor({ state: 'visible', timeout: FIND_MS });
    } catch {
      die(`not found: ${what}`);
    }
    const box = await locator.boundingBox();
    if (!box) die(`not on screen: ${what}`);
    return { x: box.x + box.width / 2, y: box.y + box.height / 2 };
  };
  return {
    at,
    glide,
    centre,
    /** Glide to the centre of `locator` over `ms`, rest a beat, click. */
    async click(locator, what, { ms = 650 } = {}) {
      const c = await centre(locator, what);
      await glide(c.x, c.y, ms);
      await sleep(140);
      await page.mouse.down();
      await sleep(90);
      await page.mouse.up();
    }
  };
}

/** Wait for `test` (run in the page) to hold, or fail naming `what`. */
async function until(page, test, arg, what) {
  try {
    await page.waitForFunction(test, arg, { timeout: FIND_MS });
  } catch {
    die(`${what} never happened (at ${page.url()})`);
  }
}

/**
 * The page is in a state worth recording, then one fresh frame of it is taken:
 * `want` visible, the header's tab for the route active with its underline
 * finished moving, no reading still "not read yet", no error page or logged
 * error, no sideways scroll, no line of text cut.
 */
async function settled(page, errors, cast, want, what) {
  try {
    await want.waitFor({ state: 'visible', timeout: FIND_MS });
  } catch {
    die(`not found: ${what} (at ${page.url()})`);
  }
  const tab = new URL(page.url()).pathname.startsWith('/epics') ? 'Epics' : 'Work';
  await until(
    page,
    (label) => {
      const tabs = [...document.querySelectorAll('nav[aria-label="Factory views"] a')];
      // No tab at all is a renamed nav, not a settled one: keep waiting, then fail.
      if (!tabs.some((a) => a.textContent.trim() === label)) return false;
      return tabs.every((a) => {
        const active = a.textContent.trim() === label;
        // `rgb(r, g, b)` is opaque; `rgba(r, g, b, a)` carries the fade's alpha.
        const channels = getComputedStyle(a).borderBottomColor.match(/[\d.]+/g) ?? [];
        const opacity = channels.length === 4 ? Number(channels[3]) : 1;
        return (a.getAttribute('aria-current') === 'page') === active && (active ? opacity === 1 : opacity === 0);
      });
    },
    tab,
    `the ${tab} tab's underline settling`
  );
  await until(page, () => !document.body.innerText.includes('not read yet'), undefined, 'every reading being read');
  if (await page.locator('[data-error-page]').count()) die(`the error page is showing at ${page.url()}`);
  if (await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth)) {
    die(`the page scrolls sideways at ${page.url()}`);
  }
  const cut = await page.evaluate(cutLines);
  if (cut.length) die(`text is cut at ${page.url()}: ${cut.join('; ')}`);
  if (errors.length) die(`the page reported:\n  ${errors.join('\n  ')}`);
  await cast.refresh();
}

/** The take. Returns the screencast marks where the GIF starts and ends. */
async function walk(page, base, errors, cast) {
  const p = pointer(page);
  const header = page.locator('header');
  const detail = page.locator('[data-tour="work-detail"]');
  const list = page.locator('[data-tour="work-list"]');
  const graph = page.locator('[data-tour="epic-graph"]');
  const node = (id) => graph.locator(`.svelte-flow__node[data-id="${id}"]`);
  const rest = { x: 860, y: 640 };

  // 1. Work: what needs me, and what do I run?
  await page.goto(`${base}/work`, { waitUntil: 'load' });
  await settled(page, errors, cast,detail.getByText('needs you').first(), 'the Work view\'s "needs you" list');
  await page.mouse.move(rest.x, rest.y);
  Object.assign(p.at, rest);
  await sleep(AFTER_NAV_MS);
  const start = cast.mark();
  await sleep(1150);

  // 2. The epic: Epics, then ledger-v2. The graph is the hero.
  const epicRow = page.locator('main a[href="/epics/ledger-v2"]').filter({ hasText: 'ledger-v2' });
  await p.click(header.getByRole('link', { name: 'Epics', exact: true }), 'the Epics tab');
  await settled(page, errors, cast,epicRow, 'the ledger-v2 row');
  await sleep(AFTER_NAV_MS);
  await p.click(epicRow, 'the ledger-v2 row', { ms: 600 });
  await settled(page, errors, cast,node('101'), 'node #101 in the graph');
  await sleep(AFTER_NAV_MS);
  // One slow pass across the cards, then back to the merged node.
  const across = await p.centre(node('108'), 'node #108');
  await p.glide(across.x - 40, across.y + 10, 1000);
  await sleep(200);

  // 3. A merged node's proof: its card opens in place, then its evidence in Work.
  const openInWork = page.getByRole('dialog').getByRole('link', { name: /Open in Work/ });
  await p.click(node('101'), 'node #101', { ms: 900 });
  await settled(page, errors, cast,openInWork, 'the #101 popover');
  await sleep(1250);
  await p.click(openInWork, 'Open in Work', { ms: 500 });
  const passed = detail.getByText(/Merge verify passed/);
  await settled(page, errors, cast,passed, "#101's passed merge verify");
  await sleep(300);
  const proof = await p.centre(passed, 'the merge verify line');
  await p.glide(proof.x + 140, proof.y + 4, 700);
  await sleep(1450);

  // 4. The refusal, one click away: #107's merge verify failed and the merge is held for a person.
  const failed = detail.getByText(/Merge verify failed at/).first();
  await p.click(list.locator('a[data-item="107"]'), 'the #107 row', { ms: 650 });
  await settled(page, errors, cast,failed, "#107's failed merge verify");
  await sleep(300);
  const refused = await p.centre(failed, 'the refusal sentence');
  await p.glide(refused.x + 180, refused.y + 6, 700);
  await sleep(1300);

  // 5. Back to Work, the opening view; the pointer comes home along the header, clear of the list.
  await p.click(header.getByRole('link', { name: 'Work', exact: true }), 'the Work tab', { ms: 650 });
  await settled(page, errors, cast,detail.getByText('needs you').first(), 'the Work view');
  if (new URL(page.url()).searchParams.has('item')) die(`the Work tab kept a selection: ${page.url()}`);
  await p.glide(600, 112, 350);
  await p.glide(rest.x, rest.y, 450);
  await sleep(700);
  const end = cast.mark();
  if (errors.length) die(`the page reported:\n  ${errors.join('\n  ')}`);
  return { start, end };
}

/** A concat list that holds, at each tick of `fps`, the last frame painted by then. */
function resample(frames, { start, end }, fps) {
  const lines = ['ffconcat version 1.0'];
  let i = 0;
  const ticks = Math.round((end - start) * fps);
  let last = null;
  for (let k = 0; k < ticks; k += 1) {
    const t = start + k / fps;
    while (i + 1 < frames.length && frames[i + 1].ts <= t) i += 1;
    if (frames[i].ts > t) die('the screencast has no frame from before the take started');
    last = frames[i].file;
    lines.push(`file '${last}'`, `duration ${(1 / fps).toFixed(6)}`);
  }
  lines.push(`file '${last}'`);
  return { text: `${lines.join('\n')}\n`, seconds: ticks / fps };
}

function ffmpeg(bin, args) {
  const run = spawnSync(bin, ['-hide_banner', '-loglevel', 'error', '-y', ...args], { encoding: 'utf8', maxBuffer: 64 * 1024 * 1024 });
  if (run.status !== 0) die(`ffmpeg exited ${run.status}: ${run.stderr}`);
}

async function main() {
  const { out } = parseArgs(process.argv.slice(2));
  if (!existsSync(fileURLToPath(new URL('index.html', DEMO_BUILD_DIR)))) {
    die('build-demo/ has no index.html: run `npm run build:demo` first');
  }
  const ff = ffmpegBinary();
  const executablePath = chromiumBinary();
  held.work = mkdtempSync(join(tmpdir(), 'kew-record-demo-'));
  const work = held.work;
  try {
    held.server = await serveStatic(DEMO_BUILD_DIR);
    // Playwright's own signal handlers are off: `onSignal` closes the browser with the rest.
    held.browser = await chromium.launch({ executablePath, handleSIGINT: false, handleSIGTERM: false, handleSIGHUP: false });
    const context = await held.browser.newContext({ viewport: VIEWPORT, colorScheme: 'dark', deviceScaleFactor: 1 });
    await context.addInitScript(drawPointer);
    const page = await context.newPage();
    const errors = watchForErrors(page, held.server.base);
    const cast = await startFrames(page, work);
    const take = await walk(page, held.server.base, errors, cast);
    await sleep(200);
    await cast.stop();
    await closeBrowserAndServer();
    const frames = cast.frames.filter((f) => f.ts <= take.end);
    const list = resample(frames, take, GIF_FPS);
    writeFileSync(join(work, 'frames.txt'), list.text);
    const scale = `fps=${GIF_FPS},scale=${GIF_WIDTH}:-1:flags=lanczos`;
    const palette = join(work, 'palette.png');
    ffmpeg(ff, ['-f', 'concat', '-safe', '0', '-i', join(work, 'frames.txt'), '-vf', `${scale},palettegen=stats_mode=diff`, palette]);
    const gif = join(work, 'demo-console.gif');
    ffmpeg(ff, [
      '-f', 'concat', '-safe', '0', '-i', join(work, 'frames.txt'), '-i', palette,
      '-lavfi', `${scale} [x]; [x][1:v] paletteuse=dither=${DITHER}:diff_mode=rectangle`,
      '-loop', '0', gif
    ]);
    const bytes = statSync(gif).size;
    if (bytes > MAX_BYTES) die(`the GIF is ${(bytes / 1048576).toFixed(2)} MB, over the 5 MB cap: shorten the holds`);
    // ffmpeg ran synchronously, so a signal sent meanwhile is still pending:
    // let the loop deliver it, and write nothing if one arrived.
    await new Promise((resolve) => setImmediate(resolve));
    if (held.signalled) return;
    mkdirSync(dirname(out), { recursive: true });
    // A copy, not a rename: the temp folder can be on another filesystem.
    copyFileSync(gif, out);
    console.log(
      `record-demo: ${out}: ${(bytes / 1048576).toFixed(2)} MB, ${GIF_WIDTH} px wide, ${GIF_FPS} fps, ` +
        `${list.seconds.toFixed(1)} s, from ${frames.length} painted frames`
    );
  } finally {
    await cleanup();
  }
}

/** What a run holds open; `cleanup` releases each once, on success, failure or a signal. */
const held = { server: null, browser: null, work: null, signalled: null };

async function closeBrowserAndServer() {
  const { browser, server } = held;
  Object.assign(held, { browser: null, server: null });
  await browser?.close().catch(() => {});
  await server?.close();
}

async function cleanup() {
  await closeBrowserAndServer();
  if (held.work) rmSync(held.work, { recursive: true, force: true });
  held.work = null;
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  for (const [signal, code] of [['SIGINT', 130], ['SIGTERM', 143]]) {
    process.once(signal, () => {
      held.signalled = code;
      console.error(`record-demo: ${signal}: cleaning up, no GIF written`);
      cleanup().finally(() => process.exit(code));
    });
  }
  try {
    await main();
  } catch (error) {
    // After a signal the take fails because its browser was closed: the signal's exit code stands.
    if (!held.signalled) console.error(`record-demo: ${error instanceof RecordError ? error.message : (error?.stack ?? error)}`);
    process.exit(held.signalled ?? 1);
  }
}
