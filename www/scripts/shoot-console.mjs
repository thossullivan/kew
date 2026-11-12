#!/usr/bin/env node
/**
 * Capture the front page's console frames from a demo build.
 *
 *   node scripts/shoot-console.mjs <demo-base-url>
 *
 * Serve a demo build first (in frontend/: `npx vite preview --config
 * demo/vite.demo.config.ts --port 4184 --strictPort --host 127.0.0.1`), then
 * pass its address. Every capture in src/lib/data/console-frames.js is opened
 * with reduced motion and written as a PNG to static/console/<file>, clipped
 * so the demo's own banner and its build-history foot are not in the picture:
 *   - FRAMES, the desktop frames, at 1440x900, device scale 1;
 *   - PHONE, the live view at 390x844, device scale 2;
 *   - JUDGMENT, the live view's "needs you" block, whole: in a 390x844
 *     window at device scale 2, where the console lays the block out at
 *     full height, scrolled into view and clipped to its box plus a margin.
 *     A block whose clip is not JUDGMENT's width and height is refused, so a
 *     console change shows up before it reaches the page.
 * A frame whose path selects a node opens that node's popover; Escape closes
 * it and keeps the selection.
 *
 * A capture is written only when the page threw no error and no tour is
 * open; otherwise the script names it, leaves the committed file as it is,
 * and exits 1. The PNGs are committed, like static/og.png: the build never
 * runs this. After a capture, read every frame again before keeping its label
 * in console-frames.js.
 *
 * Uses the playwright-core this package already has and the Chromium
 * browser-check.mjs finds (scripts/chromium.mjs); `$KEW_UI_CHECK_CHROMIUM`,
 * which make-og.mjs also reads, names another binary.
 */
import { mkdirSync, writeFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { CLIP, FRAMES, JUDGMENT, PHONE, VIEWPORT } from '../src/lib/data/console-frames.js';
import { chromiumBinary } from './chromium.mjs';

const OUT = resolve(dirname(fileURLToPath(import.meta.url)), '../static/console');

/** Every capture: its file, its path, its window, its scale and its clip. */
const SHOTS = [
  ...FRAMES.map((frame) => ({ file: frame.file, path: frame.path, viewport: VIEWPORT, scale: 1, clip: CLIP })),
  { file: PHONE.file, path: PHONE.path, viewport: PHONE.viewport, scale: PHONE.scale, clip: PHONE.clip },
  { file: JUDGMENT.file, path: JUDGMENT.path, viewport: JUDGMENT.viewport, scale: JUDGMENT.scale, block: true }
];

/**
 * The clip of the live view's whole "needs you" block, scrolled into view:
 * its box with JUDGMENT.margin around it, or why it cannot be taken.
 */
async function blockClip(page) {
  const found = await page.evaluate(async (margin) => {
    const summary = document.querySelector('[data-needs-you-summary]');
    const block = summary?.closest('section');
    if (!block) return { why: 'the live view shows no needs-you block' };
    block.scrollIntoView({ block: 'center', behavior: 'instant' });
    await new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)));
    const box = block.getBoundingClientRect();
    const high = document.documentElement.clientHeight;
    if (box.top - margin < 0 || box.bottom + margin > high) {
      return { why: `the block (${Math.round(box.height)} px tall) does not fit the window` };
    }
    return {
      clip: {
        x: Math.round(box.left - margin),
        y: Math.round(box.top - margin),
        width: Math.round(box.width + 2 * margin),
        height: Math.round(box.height + 2 * margin)
      }
    };
  }, JUDGMENT.margin);
  if (found.why) return found;
  if (found.clip.width !== JUDGMENT.width || found.clip.height !== JUDGMENT.height) {
    return {
      why: `the block's clip is ${found.clip.width}x${found.clip.height}, console-frames.js says ${JUDGMENT.width}x${JUDGMENT.height}; read the new block and update JUDGMENT`
    };
  }
  return found;
}

/** Why a page may not be captured, or null: a page error, a tour, a dialog left open. */
async function refusal(page, errors) {
  if (errors.length > 0) return `${errors.length} page error(s): ${errors[0]}`;
  const open = await page.evaluate(() => ({
    tour: document.querySelectorAll('.kew-tour, [data-tour-step]').length,
    dialogs: document.querySelectorAll('[role="dialog"]').length
  }));
  if (open.tour > 0) return 'a tour is open';
  if (open.dialogs > 0) return `${open.dialogs} dialog(s) are still open`;
  return null;
}

async function main() {
  const base = process.argv[2];
  if (!base || !/^https?:\/\//.test(base)) {
    console.error('usage: node scripts/shoot-console.mjs <demo-base-url>');
    return 2;
  }
  const executablePath = process.env.KEW_UI_CHECK_CHROMIUM || chromiumBinary();
  if (!executablePath) {
    console.error(
      'shoot-console: no Chromium found; install one with `npx playwright-core install chromium-headless-shell`'
    );
    return 2;
  }
  const { chromium } = await import('playwright-core');
  mkdirSync(OUT, { recursive: true });
  const browser = await chromium.launch({ executablePath });
  let failed = 0;
  try {
    for (const shot of SHOTS) {
      const context = await browser.newContext({
        viewport: shot.viewport,
        deviceScaleFactor: shot.scale,
        reducedMotion: 'reduce'
      });
      const page = await context.newPage();
      const errors = [];
      page.on('pageerror', (error) => errors.push(String(error)));
      await page.goto(new URL(shot.path, base).href, { waitUntil: 'networkidle' });
      await page.evaluate(() => document.fonts.ready);
      // The graph centres itself on its selected node after the first paint.
      await page.waitForTimeout(1500);
      // A selected node opens its popover; Escape closes it and keeps the selection.
      if (await page.locator('[role="dialog"]').count()) {
        await page.keyboard.press('Escape');
        await page.waitForTimeout(500);
      }
      let why = await refusal(page, errors);
      let clip = shot.clip;
      if (why === null && shot.block) {
        const found = await blockClip(page);
        why = found.why ?? null;
        clip = found.clip;
      }
      if (why !== null) {
        failed++;
        console.error(`FAIL ${shot.file} from ${shot.path}: ${why}; static/console/${shot.file} is left as it was`);
      } else {
        const png = await page.screenshot({ clip, animations: 'disabled' });
        writeFileSync(join(OUT, shot.file), png);
        console.log(`wrote static/console/${shot.file} from ${shot.path}`);
      }
      await context.close();
    }
  } finally {
    await browser.close();
  }
  return failed === 0 ? 0 : 1;
}

process.exitCode = await main();
