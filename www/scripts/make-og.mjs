#!/usr/bin/env node
/**
 * Render the social card to `static/og.png`.
 *
 * Every share of kew.dev on HN, LinkedIn, X or Slack renders this image, so it
 * is the most-seen surface the site has and it should be the same product as
 * the page. It is drawn from `src/lib/styles/tokens.css` — the generated
 * transcript of `frontend/src/app.css` — so it cannot drift off-palette, and
 * the two IBM Plex faces are inlined from node_modules so the render does not
 * depend on what fonts the rendering machine happens to have.
 *
 * The output is committed. Generating it in `npm run build` would put a
 * Playwright browser download in the path of every build and every CI run, to
 * regenerate a file that changes when the wording changes and at no other time.
 * Run `npm run og` when the card changes.
 *
 * 1200x630 is the size every platform crops from.
 */

import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

// Imported lazily and deliberately not a devDependency: adding it would put a
// browser download in front of every `npm ci`, including CI's, to support a
// script that runs when the card's wording changes and never otherwise. The
// generated PNG is committed, so nobody needs this to build or serve the site.
//
// Where `playwright` is absent, KEW_UI_CHECK_CHROMIUM may name a Chromium
// binary (the one browser-check.mjs finds in the Playwright cache) for the
// playwright-core this package already has.
let chromium;
let launchOptions = {};
try {
  ({ chromium } = await import('playwright'));
} catch {
  if (process.env.KEW_UI_CHECK_CHROMIUM) {
    ({ chromium } = await import('playwright-core'));
    launchOptions = { executablePath: process.env.KEW_UI_CHECK_CHROMIUM };
  }
}
if (!chromium) {
  console.error(
    '\nog: playwright is not installed here.\n\n' +
      '    npm install --no-save playwright && npm run og\n\n' +
      'It is intentionally not a devDependency — static/og.png is committed, so\n' +
      'this is only needed when the card itself changes.\n'
  );
  process.exit(1);
}

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, '..');

const WIDTH = 1200;
const HEIGHT = 630;

/** One `@font-face` with the woff2 inlined as base64. */
async function face(family, weight, file) {
  const raw = await readFile(resolve(root, 'node_modules', file));
  return (
    `@font-face{font-family:'${family}';font-style:normal;font-weight:${weight};` +
    `font-display:block;src:url(data:font/woff2;base64,${raw.toString('base64')}) format('woff2');}`
  );
}

const tokens = await readFile(resolve(root, 'src/lib/styles/tokens.css'), 'utf8');

const fonts = (
  await Promise.all([
    face('IBM Plex Mono', 400, '@fontsource/ibm-plex-mono/files/ibm-plex-mono-latin-400-normal.woff2'),
    face('IBM Plex Mono', 600, '@fontsource/ibm-plex-mono/files/ibm-plex-mono-latin-600-normal.woff2'),
    face('IBM Plex Sans', 400, '@fontsource/ibm-plex-sans/files/ibm-plex-sans-latin-400-normal.woff2')
  ])
).join('\n');

// The cursor is the wordmark's whole idea (#1128): a blinking block is the
// universal signal for "waiting for a human". Static here — it is a still.
const html = `<!doctype html>
<html><head><meta charset="utf-8"><style>
${fonts}
${tokens}
* { box-sizing: border-box; margin: 0; }
html, body { width: ${WIDTH}px; height: ${HEIGHT}px; }
body {
  background: var(--bg);
  color: var(--fg);
  font-family: 'IBM Plex Sans', sans-serif;
  padding: 76px 96px;
  display: flex;
  flex-direction: column;
  position: relative;
  overflow: hidden;
}
/* The mark and thesis sit optically centred in the space above the rule; the
   footer stays pinned. Deliberately no proof figure here — a number baked into
   a committed PNG goes stale silently, which is the one thing this site must
   not do. */
.top { margin-block: auto; }
/* A single soft bloom behind the mark, so the card is not a flat rectangle.
   Depth by value, as on the page: magenta is the person's part alone. */
body::before {
  content: '';
  position: absolute;
  top: -220px; left: -160px;
  width: 780px; height: 780px;
  background: radial-gradient(circle, var(--surface-2) 0%, transparent 62%);
  pointer-events: none;
}
.top, .bottom { position: relative; }
.mark {
  font-family: 'IBM Plex Mono', monospace;
  font-weight: 600;
  font-size: 92px;
  letter-spacing: -0.045em;
  line-height: 1;
  display: flex;
  align-items: center;
}
.cursor {
  display: inline-block;
  width: 0.52em; height: 0.88em;
  background: var(--accent-2);
  margin-left: 0.1em;
}
.thesis {
  font-size: 44px;
  line-height: 1.24;
  letter-spacing: -0.02em;
  margin-top: 34px;
  max-width: 20ch;
}
.thesis .keep { color: var(--accent); }
/* The rule, as the page's second headline (design review c9, V1). */
.proof {
  font-size: 26px;
  line-height: 1.3;
  color: var(--fg-dim);
  margin-top: 22px;
}
.rule { height: 1px; background: var(--border-strong); margin-bottom: 26px; }
.bottom { display: flex; justify-content: space-between; align-items: baseline; }
.sub {
  font-family: 'IBM Plex Mono', monospace;
  font-size: 21px;
  color: var(--muted);
  letter-spacing: -0.01em;
}
.host {
  font-family: 'IBM Plex Mono', monospace;
  font-size: 21px;
  color: var(--fg-dim);
}
</style></head><body>
  <div class="top">
    <div class="mark">kew<span class="cursor"></span></div>
    <div class="thesis">Offload the work.<br>Keep the <span class="keep">judgment</span>.</div>
    <div class="proof">A change never defines its own proof.</div>
  </div>
  <div class="bottom-wrap">
    <div class="rule"></div>
    <div class="bottom">
      <div class="sub">The control plane for autonomous coding agents</div>
      <div class="host">kew.dev</div>
    </div>
  </div>
</body></html>`;

const browser = await chromium.launch(launchOptions);
const page = await browser.newPage({ viewport: { width: WIDTH, height: HEIGHT } });

const errors = [];
page.on('pageerror', (e) => errors.push(e.message));

await page.setContent(html, { waitUntil: 'load' });
await page.evaluate(() => document.fonts.ready);

const out = resolve(root, 'static/og.png');
await page.screenshot({ path: out, type: 'png' });
await browser.close();

if (errors.length) {
  console.error('og: page errors during render:\n  ' + errors.join('\n  '));
  process.exit(1);
}

const { size } = await import('node:fs').then((fs) => fs.promises.stat(out));
console.log(`og: wrote static/og.png (${WIDTH}x${HEIGHT}, ${(size / 1024).toFixed(1)} KB)`);
