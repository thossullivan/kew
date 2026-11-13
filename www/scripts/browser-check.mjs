/**
 * The kew.dev front page fits the screen at every width (#3351).
 *
 * The bar and the hero shrank to their content: `.shell` centres itself with
 * `margin: 0 auto`, which inside the hero stage's flex column turns off
 * stretch, so on a 390 px phone the headline, intro and install command ran
 * off the right edge and the page scrolled 296 px sideways, while on desktop
 * the nav sat in a narrow centred strip. The nav was hidden at 700 px and
 * below, the headline wrapped to four lines at 1440 px, and the fleet card ran
 * off a phone.
 *
 * Over a production build (`npm run build` writes `build/`), served as Caddy
 * serves it, at 320, 360, 390, 768, 1080, 1200 and 1440 px, and at 1366x650
 * for the layout checks:
 *   - the page answers 200 and every request it makes succeeds;
 *   - the page's scroll width is the viewport width, and neither html nor
 *     body hides or clips overflow-x, which would hide a wide page from that
 *     measure;
 *   - no visible element ends past the viewport's right edge, or starts past
 *     its left edge, unless an element inside the body scrolls it sideways;
 *   - no element that hides or clips its overflow-x cuts content off, save
 *     the anchors' canonical records, which end in an ellipsis;
 *   - at 1440 the bar and the hero are as wide as the shell;
 *   - the nav shows Docs, How a change is proven, Demo and GitHub, each a tap
 *     target at least 44 px on a side that a user sees at the top of the
 *     page: displayed, visible, opaque, not clipped, inside the viewport and
 *     not covered by anything; and holds them on one row (#3354);
 *   - at 1080 and 1440 "Offload the work." is drawn on one line;
 *   - at every width the hero's proof block fits with no sideways scroll
 *     inside, states the rule as its second headline, draws the always-on
 *     rows of PROOF_CHANNELS in order, folds the opt-in rows under the one
 *     summary line, and links the proof and threat-model docs; the hero's
 *     copy and rule hold no button, so they are complete at rest, and its
 *     actions are Install kew and the text link How a change is proven;
 *   - at 1366x650 the rule, the three always-on rows, the folded row's
 *     summary and both doc links are inside the first viewport;
 *   - from 640 px the console figure under the hero draws the live frame
 *     and a time rail of four stops, 44 px buttons labelled with their time
 *     and moment, each dot at its frame's replay clock, no two labels
 *     touching; the live stop is pressed and cyan; pressing each stop draws
 *     its frame alone and moves nothing on the page. Below 640 px it draws
 *     one phone capture and no rail, and each width fetches only its own
 *     image (every image is sized, lazy, async, with its alt);
 *   - from 1000 px the live view's needs-you block hangs under stage 05,
 *     starting at its column, with the strip one row; narrower it is
 *     neither drawn nor fetched;
 *   - the share bar and the spend bar name, in their aria-label, the figures
 *     worked out from repo-stats.json and the pack's raw numbers, and each
 *     part is that share of its bar;
 *   - the per-model fold is folded at rest under a 44 px summary, and opened
 *     shows the scorecard's table and its three notes without a sideways
 *     scroll;
 *   - the loop's six stages read in order, 01 and 05 in magenta and marked
 *     "· you", in six columns, three on a tablet and one on a phone; the
 *     contained route is four boxes and three arrows, the first dashed and
 *     Untrusted, in one row from 900 px and stacked below, linked to the
 *     proof docs;
 *   - the install command reads exactly INSTALL on one line at every width,
 *     never broken inside a token; its block runs the column's full width and
 *     scrolls the command sideways inside itself;
 *   - every font face the page loads decodes (none has status "error");
 *   - the install section's Copy puts exactly INSTALL on the clipboard;
 *   - at 390 and 1440 every small visible text node, on the page and in the
 *     opened per-model fold, reads at least 4.5:1 against what is behind it
 *     (#3353);
 *   - the demo (#3354) is linked once each from the nav and the console
 *     figure ("Open the live demo ↗"); with script, each opens the needs-you
 *     tour from 1024 px (checked at 1023 and 1024 px as well) and the start
 *     page below it, and without script the start page. The tour id and the
 *     breakpoint are checked against the demo's own sources by
 *     tests/demo-tour-link.test.mjs;
 *   - with script, the anchors verify in the browser, break when the first or
 *     the last is altered, and restore; the chain is drawn between the cards
 *     (across the gap in a row, down the left edge stacked), green, and the
 *     link an alter breaks turns red with a cross until restored;
 *   - without Web Crypto the anchors say the browser cannot recompute and
 *     every alter is disabled; a recompute that fails while an anchor is
 *     altered returns every row to the build's;
 *   - the install block fades at its right edge until scrolled to its end,
 *     takes keyboard focus and is named;
 *   - Demo and GitHub in the nav and GitHub in the footer carry "↗", and no
 *     footer dot ends a line;
 *   - a link to /#loop or /#install lands on its section, and /#%E0 throws no
 *     page error;
 *   - with script off, at 390 and 1440, every anchor shows its computed
 *     hashes under a "chain intact" status, the console figure shows the
 *     live frame alone and no rail, and the per-model fold is there;
 *   - no page throws an uncaught error (a pageerror listener on every pass);
 *   - one pass at 1440 runs with motion allowed: nothing on the page advances
 *     by itself, and at rest it asks for no animation frames.
 *
 * Prints PASS or FAIL per check and size; exits 1 on any failure, 2 when there
 * is no build or no Chromium to run it in. `--shots <dir>` also writes a
 * full-page screenshot per size, for the operator's visual ruling.
 *
 * Not part of `npm run gates`: the www CI job has no browser.
 */
import { createReadStream, existsSync, mkdirSync, statSync } from 'node:fs';
import { createServer } from 'node:http';
import { extname, join, normalize, resolve, sep } from 'node:path';

import {
  DEFAULT_FRAME,
  END_MINUTES,
  FRAME_DIR,
  FRAME_HEIGHT,
  FRAME_WIDTH,
  FRAMES,
  JUDGMENT,
  PHONE
} from '../src/lib/data/console-frames.js';
import { DEMO_TOUR_URL, DEMO_URL } from '../src/lib/data/demo.js';
import { INSTALL } from '../src/lib/data/install.js';
import {
  PROOF_ALWAYS,
  PROOF_DOCS,
  PROOF_OPT_IN,
  PROOF_OPT_IN_SUMMARY,
  PROOF_RULE,
  THREAT_MODEL_DOCS
} from '../src/lib/data/proof.js';
import { PACK, RUN_KINDS, SCORECARD } from '../src/lib/data/pack-figures.js';
import STATS from '../src/lib/data/repo-stats.json' with { type: 'json' };
import { chromiumBinary } from './chromium.mjs';
import { AA, hex, textContrast } from './contrast.mjs';

const ROOT = resolve('build');
const SIZES = [
  { width: 320, height: 640 },
  { width: 360, height: 740 },
  { width: 390, height: 844 },
  { width: 768, height: 1024 },
  { width: 1080, height: 800 },
  { width: 1200, height: 860 },
  { width: 1440, height: 960 }
];
/** The tour's 1024 px breakpoint from both sides (TOUR_FLOOR below), run for
 *  the demo-link check only. */
const TOUR_EDGE_SIZES = [
  { width: 1023, height: 800 },
  { width: 1024, height: 800 }
];
/** A short laptop window (design review c9, L4), run for the first-viewport
 *  check and the layout checks only. */
const LAPTOP = { width: 1366, height: 650 };
/** The one pass with motion allowed: every other pass runs with reduced
 *  motion, which would hide anything that advances by itself. */
const MOTION = { width: 1440, height: 900 };
const SHELL = 1320;
const NAV = ['Docs', 'How a change is proven', 'GitHub'];
const TAP = 44;
const HEADLINE = 'Offload the work.';

// --- the static server -----------------------------------------------------

const TYPES = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.ico': 'image/x-icon',
  '.woff': 'font/woff',
  '.woff2': 'font/woff2',
  '.txt': 'text/plain; charset=utf-8'
};

/** The file under ROOT a path names, its `index.html` for a directory, else
 *  none. A path that would leave ROOT names nothing. */
function fileFor(pathname) {
  let decoded;
  try {
    decoded = decodeURIComponent(pathname);
  } catch {
    return null;
  }
  let path = normalize(join(ROOT, decoded));
  if (path !== ROOT && !path.startsWith(`${ROOT}${sep}`)) return null;
  if (existsSync(path) && statSync(path).isDirectory()) path = join(path, 'index.html');
  return existsSync(path) && statSync(path).isFile() ? path : null;
}

function serve() {
  const server = createServer((request, response) => {
    const path = fileFor(new URL(request.url ?? '/', 'http://localhost').pathname);
    if (path === null) {
      response.writeHead(404).end();
      return;
    }
    response.writeHead(200, {
      'Content-Type': TYPES[extname(path)] ?? 'application/octet-stream'
    });
    createReadStream(path).pipe(response);
  });
  return new Promise((resolveServer, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', () => resolveServer(server));
  });
}

// --- the checks ------------------------------------------------------------
// Each takes the page at one size and returns null on a pass, or why not.

/** The page answered 2xx and no request it made failed or answered an error. */
async function everyRequestSucceeds(page, size, run) {
  const problems = [];
  if (run.status === null || run.status < 200 || run.status > 299) {
    problems.push(`the page answered ${run.status ?? 'nothing'}`);
  }
  problems.push(...run.failures);
  return problems.length === 0 ? null : problems.join('; ');
}

/** No sideways scroll: the document is exactly as wide as the viewport, and
 *  html and body do not hide or clip overflow-x, which makes that hold
 *  however wide the content is. */
async function noSidewaysScroll(page) {
  const found = await page.evaluate(() => ({
    scroll: document.documentElement.scrollWidth,
    client: document.documentElement.clientWidth,
    html: getComputedStyle(document.documentElement).overflowX,
    body: getComputedStyle(document.body).overflowX
  }));
  const problems = [];
  if (found.scroll !== found.client) {
    problems.push(`scroll width ${found.scroll} px against a ${found.client} px viewport`);
  }
  for (const name of ['html', 'body']) {
    if (found[name] === 'hidden' || found[name] === 'clip') {
      problems.push(`${name} has overflow-x: ${found[name]}`);
    }
  }
  return problems.length === 0 ? null : problems.join('; ');
}

/** No visible element runs past one edge of the viewport ('left' or 'right'),
 *  unless an element between it and the body scrolls sideways (overflow-x
 *  auto or scroll). An overflow-x hidden or clip parent does not excuse it:
 *  that cuts content off. */
async function pastEdge(page, side) {
  const offenders = await page.evaluate((edge) => {
    const client = document.documentElement.clientWidth;
    const scrolls = (el) => {
      const x = getComputedStyle(el).overflowX;
      return x === 'auto' || x === 'scroll';
    };
    const name = (el) => {
      const id = el.id ? `#${el.id}` : '';
      const classes = [...el.classList]
        .filter((c) => !c.startsWith('svelte-'))
        .map((c) => `.${c}`)
        .join('');
      return `${el.tagName.toLowerCase()}${id}${classes}`;
    };
    const past = (box) => (edge === 'right' ? box.right > client + 0.5 : box.left < -0.5);
    const found = [];
    for (const el of document.body.querySelectorAll('*')) {
      const box = el.getBoundingClientRect();
      if (box.width === 0 || !past(box)) continue;
      const style = getComputedStyle(el);
      if (style.visibility === 'hidden' || style.display === 'none') continue;
      let scroller = false;
      for (let up = el.parentElement; up && up !== document.body; up = up.parentElement) {
        if (scrolls(up)) {
          scroller = true;
          break;
        }
      }
      if (scroller) continue;
      const at = Math.round(edge === 'right' ? box.right : box.left);
      found.push(`${name(el)} ${edge === 'right' ? 'ends' : 'starts'} at ${at} px`);
    }
    return { client, found };
  }, side);
  if (offenders.found.length === 0) return null;
  const shown = offenders.found.slice(0, 4).join(', ');
  const more = offenders.found.length > 4 ? ` and ${offenders.found.length - 4} more` : '';
  return `in a ${offenders.client} px viewport ${shown}${more}`;
}

const nothingPastRightEdge = (page) => pastEdge(page, 'right');
const nothingPastLeftEdge = (page) => pastEdge(page, 'left');

/** Elements that cut their text off on purpose, with an ellipsis: the
 *  evidence section's canonical anchor records. */
const ELLIPSIS = ['#evidence code.a-canon'];

/** Nothing is cut off inside the viewport: every visible element whose
 *  overflow-x is hidden or clip holds its content (scroll width within 1 px
 *  of its client width), except the ELLIPSIS elements, and those only while
 *  they draw an ellipsis where they cut. */
async function nothingClippedInside(page) {
  const offenders = await page.evaluate((allowed) => {
    const name = (el) => {
      const id = el.id ? `#${el.id}` : '';
      const classes = [...el.classList]
        .filter((c) => !c.startsWith('svelte-'))
        .map((c) => `.${c}`)
        .join('');
      return `${el.tagName.toLowerCase()}${id}${classes}`;
    };
    const found = [];
    for (const el of document.body.querySelectorAll('*')) {
      // Text for assistive tech only (.sr-only) is clipped to 1 px on purpose.
      if (el.closest('.sr-only, .sr-only-focusable:not(:focus-visible)')) continue;
      const style = getComputedStyle(el);
      if (style.overflowX !== 'hidden' && style.overflowX !== 'clip') continue;
      if (style.display === 'none' || style.visibility === 'hidden') continue;
      if (el.getBoundingClientRect().width === 0) continue;
      if (el.scrollWidth <= el.clientWidth + 1) continue;
      const intended = allowed.some((sel) => el.matches(sel)) && style.textOverflow === 'ellipsis';
      if (!intended) found.push(`${name(el)} cuts off ${el.scrollWidth - el.clientWidth} px`);
    }
    return found;
  }, ELLIPSIS);
  if (offenders.length === 0) return null;
  const more = offenders.length > 4 ? ` and ${offenders.length - 4} more` : '';
  return `${offenders.slice(0, 4).join(', ')}${more}`;
}

/** At 1440 the bar and the hero span the shell, not their content. */
async function barAndHeroFillShell(page) {
  const found = await page.evaluate(() => {
    const stage = document.querySelector('.hero-stage');
    const width = (el) => (el ? el.getBoundingClientRect().width : null);
    return {
      client: document.documentElement.clientWidth,
      bar: width(stage?.querySelector(':scope > .bar') ?? null),
      hero: width(stage?.querySelector(':scope > .hero') ?? null)
    };
  });
  const expected = Math.min(SHELL, found.client);
  const wrong = ['bar', 'hero'].filter(
    (name) => found[name] === null || Math.abs(found[name] - expected) > 1
  );
  return wrong.length === 0
    ? null
    : wrong.map((name) => `the ${name} is ${found[name]} px, the shell ${expected} px`).join('; ');
}

/** The nav shows its three links at every width, each a 44 px tap target a
 *  user can see and reach at the top of the page, and no longer carries
 *  Philosophy. Seen means: not see-through (the link and every ancestor have
 *  opacity above 0), not cut away by a clip-path or clip on it or an
 *  ancestor, wholly inside the viewport, and the topmost thing at its centre
 *  (so no overlay covers it). */
async function navShowsWithTapTargets(page) {
  return navTapTargets(page, NAV);
}

/**
 * How a user sees one element where it sits now. Runs in the page (Playwright
 * serialises it, so it uses nothing from this module). With `into`, an
 * element not wholly in the viewport is first scrolled to its centre.
 * Each way it can be missed is named with the element that causes it:
 * display: none on it or an ancestor, visibility other than visible, no size,
 * opacity 0 on it or an ancestor, a clip-path or clip on it or an ancestor,
 * outside the viewport, or covered at its centre by something else.
 */
function sightOf(el, into) {
  const name = (node) =>
    node.tagName.toLowerCase() +
    (node.id ? `#${node.id}` : '') +
    [...node.classList]
      .filter((c) => !c.startsWith('svelte-'))
      .map((c) => `.${c}`)
      .join('');
  const client = document.documentElement.clientWidth;
  const high = document.documentElement.clientHeight;
  const within = (b) => b.left >= 0 && b.right <= client && b.top >= 0 && b.bottom <= high;
  if (into && !within(el.getBoundingClientRect())) {
    el.scrollIntoView({ block: 'center', inline: 'nearest', behavior: 'instant' });
  }
  const box = el.getBoundingClientRect();
  let undisplayed = null;
  let faded = null;
  let clipped = null;
  for (let up = el; up; up = up.parentElement) {
    const style = getComputedStyle(up);
    if (undisplayed === null && style.display === 'none') undisplayed = name(up);
    if (faded === null && parseFloat(style.opacity) <= 0) faded = name(up);
    if (clipped === null && (style.clipPath !== 'none' || (style.clip !== 'auto' && style.clip !== ''))) {
      clipped = name(up);
    }
  }
  const hit = document.elementFromPoint(box.left + box.width / 2, box.top + box.height / 2);
  return {
    // A link's label without its trailing arrow: "Demo ↗" is Demo.
    text: el.textContent.replace(/[↗→]/g, '').trim(),
    undisplayed,
    visibility: getComputedStyle(el).visibility,
    faded,
    clipped,
    width: box.width,
    height: box.height,
    top: box.top,
    inside: within(box),
    onTop: hit !== null && el.contains(hit),
    hit: hit ? name(hit) : null
  };
}

/** Why `label` is not seen, from its `sightOf`: one cause when it is not drawn
 *  at all, else every cause that holds; empty when a user sees it. */
function sightProblems(label, seen) {
  if (seen.undisplayed !== null) return [`${label} is not shown: display: none on ${seen.undisplayed}`];
  if (seen.visibility !== 'visible') return [`${label} is not shown: visibility: ${seen.visibility}`];
  if (seen.width <= 0 || seen.height <= 0) {
    return [`${label} is not shown: it is ${seen.width}x${seen.height} px`];
  }
  const problems = [];
  if (seen.faded !== null) problems.push(`${label} is see-through: opacity 0 on ${seen.faded}`);
  if (seen.clipped !== null) problems.push(`${label} is cut by a clip-path or clip on ${seen.clipped}`);
  if (!seen.inside) problems.push(`${label} runs outside the viewport`);
  else if (!seen.onTop) problems.push(`${label} is covered by ${seen.hit ?? 'nothing drawn'}`);
  return problems;
}

/** Each nav link's `sightOf` at the top of the page, in order. */
async function navSights(page) {
  await page.evaluate(() => window.scrollTo({ top: 0, left: 0, behavior: 'instant' }));
  const sights = [];
  for (const link of await page.locator('.hero-stage nav a').all()) {
    sights.push(await link.evaluate(sightOf, false));
  }
  return sights;
}

/** The problems with each named nav link as a tap target, none when all hold. */
async function navTapTargets(page, names) {
  const links = await navSights(page);
  const problems = [];
  for (const name of names) {
    const link = links.find((l) => l.text === name);
    if (!link) {
      problems.push(`${name} is not shown: the nav has no such link`);
      continue;
    }
    problems.push(...sightProblems(name, link));
    if (link.width <= 0 || link.height <= 0) continue;
    if (link.height < TAP || link.width < TAP) {
      problems.push(`${name} is ${link.width.toFixed(1)}x${link.height.toFixed(1)} px`);
    }
  }
  if (links.some((l) => l.text === 'Philosophy')) problems.push('Philosophy is still in the nav');
  return problems.length === 0 ? null : problems.join('; ');
}

/** Every nav link sits on one row: their tops agree within 2 px, so the nav
 *  never wraps a link under another (#3354: GitHub dropped under Demo at 320
 *  and 360 px). */
async function navOnOneRow(page) {
  const links = await navSights(page);
  if (links.length === 0) return 'the nav has no links';
  const tops = links.map((l) => l.top);
  const spread = Math.max(...tops) - Math.min(...tops);
  if (spread <= 2) return null;
  const rows = links.map((l) => `${l.text} at ${l.top.toFixed(1)}`).join(', ');
  return `the nav wraps: its links' tops differ by ${spread.toFixed(1)} px (${rows})`;
}

/** The headline's first sentence is drawn on one line: every box the browser
 *  draws its text in shares one top. */
async function headlineHoldsFirstLine(page) {
  const found = await page.evaluate(() => {
    const h1 = document.querySelector('.hero-stage h1');
    const node = h1 ? [...h1.childNodes].find((n) => n.nodeType === Node.TEXT_NODE) : null;
    if (!node) return { text: null, lines: 0 };
    const range = document.createRange();
    range.selectNodeContents(node);
    const tops = new Set(
      [...range.getClientRects()].filter((r) => r.width > 0).map((r) => Math.round(r.top))
    );
    return { text: node.textContent.trim(), lines: tops.size };
  });
  // "the work." is held together by a no-break space, so a phone never leaves
  // "work." alone on a line; the words are the same.
  found.text = found.text?.replace(/ /g, ' ') ?? null;
  if (found.text !== HEADLINE) return `the headline starts "${found.text}", not "${HEADLINE}"`;
  return found.lines === 1 ? null : `"${HEADLINE}" is drawn on ${found.lines} lines`;
}

/** The hero's proof block fits at every width (it ends inside the viewport
 *  and its card does not scroll sideways), states the rule as its heading,
 *  draws the always-on channels in order in the open table, folds the opt-in
 *  channels in order under the one summary line in a <details>, and links
 *  the proof and threat-model docs. */
async function proofCardFits(page) {
  const found = await page.evaluate(() => {
    const block = document.querySelector('.hero-stage .proof');
    if (!block) return null;
    const card = block.querySelector('.card') ?? block;
    const text = (el) => (el ? el.textContent.replace(/\s+/g, ' ').trim() : null);
    const open = block.querySelector(':scope .card > table.channels');
    const fold = block.querySelector('details.optin');
    return {
      client: document.documentElement.clientWidth,
      right: block.getBoundingClientRect().right,
      scroll: card.scrollWidth - card.clientWidth,
      rule: text(block.querySelector('h2.rule')),
      always: open ? [...open.querySelectorAll('tbody th')].map(text) : [],
      summary: text(fold?.querySelector('summary')),
      optIn: fold ? [...fold.querySelectorAll('tbody th')].map(text) : [],
      links: [...block.querySelectorAll('a')].map((a) => a.getAttribute('href'))
    };
  });
  if (found === null) return 'no proof block in the hero';
  const problems = [];
  if (found.right > found.client) {
    problems.push(`the block ends at ${found.right} px in a ${found.client} px viewport`);
  }
  if (found.scroll > 0) problems.push(`the card scrolls ${found.scroll} px sideways inside`);
  if (found.rule !== PROOF_RULE) problems.push(`the second headline reads "${found.rule}"`);
  const always = PROOF_ALWAYS.map((row) => row.emits);
  if (JSON.stringify(found.always) !== JSON.stringify(always)) {
    problems.push(`the open table draws ${JSON.stringify(found.always)}`);
  }
  if (found.summary !== PROOF_OPT_IN_SUMMARY) {
    problems.push(`the folded row reads "${found.summary}"`);
  }
  const optIn = PROOF_OPT_IN.map((row) => row.emits);
  if (JSON.stringify(found.optIn) !== JSON.stringify(optIn)) {
    problems.push(`the fold holds ${JSON.stringify(found.optIn)}`);
  }
  for (const href of [PROOF_DOCS, THREAT_MODEL_DOCS]) {
    if (!found.links.includes(href)) problems.push(`the block does not link ${href}`);
  }
  return problems.length === 0 ? null : problems.join('; ');
}

/** At 1366x650, at the top of the page, the whole proof block is in the
 *  first viewport: the rule, the three always-on rows, the folded row's
 *  summary and both doc links (design review c9, L4). */
async function heroCardInFirstViewport(page) {
  await page.evaluate(() => window.scrollTo({ top: 0, left: 0, behavior: 'instant' }));
  const found = await page.evaluate(() => {
    const block = document.querySelector('.hero-stage .proof');
    if (!block) return null;
    const high = document.documentElement.clientHeight;
    const parts = [
      ['the rule', block.querySelector('h2.rule')],
      ...[...block.querySelectorAll(':scope .card > table.channels tbody tr')].map((tr, i) => [
        `row ${i + 1}`,
        tr
      ]),
      ['the folded row', block.querySelector('details.optin summary')],
      ...[...block.querySelectorAll('.foot a')].map((a, i) => [`doc link ${i + 1}`, a])
    ];
    return {
      high,
      rows: block.querySelectorAll(':scope .card > table.channels tbody tr').length,
      links: block.querySelectorAll('.foot a').length,
      out: parts
        .filter(([, el]) => el)
        .map(([name, el]) => [name, el.getBoundingClientRect()])
        .filter(([, box]) => box.top < 0 || box.bottom > high || box.height === 0)
        .map(([name, box]) => `${name} spans ${Math.round(box.top)} to ${Math.round(box.bottom)} px`)
    };
  });
  if (found === null) return 'no proof block in the hero';
  const problems = [];
  if (found.rows !== PROOF_ALWAYS.length) problems.push(`the open table has ${found.rows} rows`);
  if (found.links !== 2) problems.push(`the block has ${found.links} doc links`);
  for (const out of found.out) problems.push(`${out} in a ${found.high} px viewport`);
  return problems.length === 0 ? null : problems.join('; ');
}

/** Each character of the text inside every element a selector matches, with
 *  the top of the line it is drawn on (null when it draws nothing) and
 *  whether a user sees it: its box lies inside the viewport's width and
 *  inside the visible box of every ancestor whose overflow is hidden or clip. */
async function charLines(page, selector) {
  return page.evaluate((sel) => {
    const client = document.documentElement.clientWidth;
    const cuts = (style) =>
      ['hidden', 'clip'].includes(style.overflowX) || ['hidden', 'clip'].includes(style.overflowY);
    /** The visible boxes of the clipping ancestors of a text node. */
    const clipsOf = (node) => {
      const boxes = [];
      for (let up = node.parentElement; up; up = up.parentElement) {
        if (!cuts(getComputedStyle(up))) continue;
        const box = up.getBoundingClientRect();
        const left = box.left + up.clientLeft;
        const top = box.top + up.clientTop;
        boxes.push({ left, top, right: left + up.clientWidth, bottom: top + up.clientHeight });
      }
      return boxes;
    };
    const within = (r, c) =>
      r.left >= c.left - 0.5 &&
      r.right <= c.right + 0.5 &&
      r.top >= c.top - 0.5 &&
      r.bottom <= c.bottom + 0.5;
    return [...document.querySelectorAll(sel)].map((el) => {
      const found = [];
      const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
      for (let node = walker.nextNode(); node; node = walker.nextNode()) {
        const clips = clipsOf(node);
        for (let i = 0; i < node.length; i++) {
          const range = document.createRange();
          range.setStart(node, i);
          range.setEnd(node, i + 1);
          const box = [...range.getClientRects()].find((r) => r.width > 0);
          const seen =
            box !== undefined &&
            box.left >= -0.5 &&
            box.right <= client + 0.5 &&
            clips.every((c) => within(box, c));
          found.push({ ch: node.data[i], top: box ? Math.round(box.top) : null, seen });
        }
      }
      return found;
    });
  }, selector);
}

/** Every line break in the drawn characters that `preferred(text, last, next)`
 *  refuses, as the text around it; `last` and `next` are the indices of the
 *  drawn characters either side of the break. Spaces never start a line. */
function badBreaks(chars, preferred) {
  const text = chars.map((c) => c.ch).join('');
  const found = [];
  let last = -1;
  for (let i = 0; i < chars.length; i++) {
    if (chars[i].ch === ' ' || chars[i].top === null) continue;
    if (last !== -1 && chars[i].top > chars[last].top + 2 && !preferred(text, last, i)) {
      found.push(`${text.slice(Math.max(0, last - 9), last + 1)} / ${text.slice(i, i + 6)}`);
    }
    last = i;
  }
  return found;
}

// The hero's primary reads "Install kew" and links here (#3354), so the
// install section holds the page's one install command.
const COMMANDS = [['the install section', '#install .cmdline code']];

/** The install command reads exactly INSTALL and is drawn on one line at
 *  every width, so it never breaks inside a token ("kew-/cli" on a phone,
 *  design review c9, L20); a window too narrow for it scrolls it sideways
 *  inside its own block. */
async function installOnOneLine(page) {
  const problems = [];
  for (const [where, selector] of COMMANDS) {
    const [chars] = await charLines(page, selector);
    if (chars === undefined) {
      problems.push(`${where} has no install command`);
      continue;
    }
    const text = chars.map((c) => c.ch).join('');
    if (text !== INSTALL) {
      problems.push(`${where} reads "${text}"`);
      continue;
    }
    const bad = badBreaks(chars, () => false);
    for (const around of bad) problems.push(`${where} breaks at "${around}"`);
  }
  return problems.length === 0 ? null : problems.join('; ');
}

/** The install block runs the column's full width and ends inside the
 *  viewport; its command sits in a box that scrolls sideways (overflow-x
 *  auto), and scrolled to its end the command's last character is drawn
 *  inside the viewport. Copy stays beside it, in view. */
async function installScrollsInItsBlock(page) {
  const found = await page.evaluate(() => {
    const block = document.querySelector('#install .cmdline');
    const box = document.querySelector('#install .cmdscroll');
    const code = box?.querySelector('code');
    const copy = block?.querySelector('button');
    if (!block || !box || !code || !copy) return null;
    const client = document.documentElement.clientWidth;
    const column = block.parentElement.getBoundingClientRect();
    const own = block.getBoundingClientRect();
    box.scrollLeft = box.scrollWidth;
    const range = document.createRange();
    range.selectNodeContents(code);
    const rects = [...range.getClientRects()].filter((r) => r.width > 0);
    const lastRight = rects.length ? Math.max(...rects.map((r) => r.right)) : null;
    box.scrollLeft = 0;
    return {
      client,
      overflow: getComputedStyle(box).overflowX,
      blockLeft: own.left,
      blockRight: own.right,
      columnLeft: column.left,
      columnRight: column.right,
      lastRight,
      boxRight: box.getBoundingClientRect().right,
      copyRight: copy.getBoundingClientRect().right
    };
  });
  if (found === null) return 'the install section has no command block with Copy';
  const problems = [];
  if (found.overflow !== 'auto' && found.overflow !== 'scroll') {
    problems.push(`the command's box has overflow-x: ${found.overflow}`);
  }
  if (Math.abs(found.blockLeft - found.columnLeft) > 1 || Math.abs(found.blockRight - found.columnRight) > 1) {
    problems.push(
      `the block spans ${Math.round(found.blockLeft)} to ${Math.round(found.blockRight)} px, the column ${Math.round(found.columnLeft)} to ${Math.round(found.columnRight)} px`
    );
  }
  if (found.blockRight > found.client + 0.5) problems.push(`the block ends at ${found.blockRight} px`);
  if (found.copyRight > found.client + 0.5) problems.push(`Copy ends at ${found.copyRight} px`);
  if (found.lastRight === null || found.lastRight > found.boxRight + 0.5) {
    problems.push('scrolled to its end, the command still runs past its box');
  }
  return problems.length === 0 ? null : problems.join('; ');
}

/** Every font the page asked for decoded: none in document.fonts errored. */
async function fontsDecode(page) {
  const broken = await page.evaluate(() =>
    [...document.fonts]
      .filter((face) => face.status === 'error')
      .map((face) => `${face.family} ${face.weight} ${face.style}`)
  );
  if (broken.length === 0) return null;
  const unique = [...new Set(broken)];
  return `${unique.length} font face(s) failed to decode: ${unique.slice(0, 4).join(', ')}`;
}

/** The install section's Copy puts exactly INSTALL on the clipboard. */
async function copyCopiesInstall(page) {
  const button = page.locator('#install .cmdline button');
  await button.scrollIntoViewIfNeeded();
  await button.click();
  const copied = await page.evaluate(() => navigator.clipboard.readText());
  return copied === INSTALL ? null : `Copy put "${copied}" on the clipboard`;
}

/** Every small text node under `root` that a user sees and that reads below
 *  4.5:1 against what is behind it. Small is under 24 px, or under 18.66 px
 *  when bold (WCAG's large-text line). The page reports, for each node, its
 *  computed fill (`fill` inside an SVG) and every element from its parent up
 *  to the root with its background and its opacity; contrast.mjs composites
 *  them as drawn, so a stage or row dimmed by opacity is measured dimmed
 *  (#3353). Text an opacity of 0 hides is not seen. Text under `aria-hidden`
 *  is decoration and skipped, and so is a disabled control, which WCAG
 *  exempts; the grain overlay holds no text and sits beside the content,
 *  never behind it. */
async function lowContrastText(page, root) {
  const scan = await page.evaluate(
    (root) => {
      const name = (el) => {
        const id = el.id ? `#${el.id}` : '';
        const classes = [...el.classList]
          .filter((c) => !c.startsWith('svelte-'))
          .map((c) => `.${c}`)
          .join('');
        return `${el.tagName.toLowerCase()}${id}${classes}`;
      };
      const canvas = document.createElement('canvas');
      canvas.width = canvas.height = 1;
      const ctx = canvas.getContext('2d', { willReadFrequently: true });
      const parsed = new Map();
      /** Any CSS colour as straight sRGB 0-255 and alpha 0-1. */
      const rgba = (css) => {
        if (!parsed.has(css)) {
          ctx.clearRect(0, 0, 1, 1);
          ctx.fillStyle = '#000';
          ctx.fillStyle = css;
          ctx.fillRect(0, 0, 1, 1);
          const [r, g, b, a] = ctx.getImageData(0, 0, 1, 1).data;
          parsed.set(css, { r, g, b, a: a / 255 });
        }
        return parsed.get(css);
      };
      const clear = { r: 0, g: 0, b: 0, a: 0 };
      /** The text's parent up to the root, each with its background (none
       *  inside an SVG) and its opacity. */
      const chain = (el) => {
        const steps = [];
        for (let up = el; up; up = up.parentElement) {
          const style = getComputedStyle(up);
          const inSvg = up instanceof SVGElement && !(up instanceof SVGSVGElement);
          steps.push({
            background: inSvg ? clear : rgba(style.backgroundColor),
            opacity: parseFloat(style.opacity)
          });
        }
        return steps;
      };
      const nodes = [];
      const scope = document.querySelector(root);
      if (!scope) return { page: null, nodes, missing: true };
      const walker = document.createTreeWalker(scope, NodeFilter.SHOW_TEXT);
      for (let node = walker.nextNode(); node; node = walker.nextNode()) {
        const el = node.parentElement;
        if (!el || node.data.trim() === '' || el.closest('[aria-hidden="true"]')) continue;
        if (el.closest(':disabled')) continue;
        const style = getComputedStyle(el);
        if (style.visibility !== 'visible') continue;
        const range = document.createRange();
        range.selectNodeContents(node);
        if (![...range.getClientRects()].some((r) => r.width > 0 && r.height > 0)) continue;
        const size = parseFloat(style.fontSize);
        const bold = parseInt(style.fontWeight, 10) >= 700;
        if (size >= 24 || (bold && size >= 18.66)) continue;
        const svg = el instanceof SVGElement;
        nodes.push({
          label: `${name(el)} "${node.data.trim().slice(0, 24)}"`,
          fill: rgba(svg ? style.fill : style.webkitTextFillColor || style.color),
          chain: chain(el)
        });
      }
      const bg = getComputedStyle(document.documentElement).getPropertyValue('--bg');
      return { page: rgba(bg), nodes, missing: false };
    },
    root
  );
  if (scan.missing) return { seen: 0, found: [`nothing matches ${root}`] };
  let seen = 0;
  const found = [];
  for (const node of scan.nodes) {
    const drawn = textContrast(node.fill, node.chain, scan.page);
    if (drawn === null) continue;
    seen++;
    if (drawn.ratio < AA) {
      found.push(
        `${node.label} ${hex(drawn.fg)} on ${hex(drawn.bg)} is ${drawn.ratio.toFixed(2)}:1`
      );
    }
  }
  return { seen, found };
}

/** Every small visible text node on the page meets WCAG AA's 4.5:1: the page
 *  as it loads, then the evidence section's per-model fold, opened. A page
 *  with no fold fails rather than skipping it. */
async function smallTextMeetsAA(page) {
  const problems = [];
  const scans = [['the page', await lowContrastText(page, 'body')]];
  const opened = await page.evaluate(() => {
    const fold = document.querySelector('#evidence details.models');
    if (fold) fold.open = true;
    return Boolean(fold);
  });
  if (!opened) problems.push('the evidence section has no fold to open');
  else scans.push(['the per-model fold', await lowContrastText(page, '#evidence details.models')]);
  await page.evaluate(() => {
    const fold = document.querySelector('#evidence details.models');
    if (fold) fold.open = false;
  });
  for (const [where, scan] of scans) {
    if (scan.seen === 0) problems.push(`${where} shows no small text to measure`);
    if (scan.found.length === 0) continue;
    const more = scan.found.length > 5 ? ` and ${scan.found.length - 5} more` : '';
    problems.push(`${where}: ${scan.found.slice(0, 5).join(', ')}${more}`);
  }
  return problems.length === 0 ? null : problems.join('; ');
}

// --- the demo (#3354) ------------------------------------------------------

/** The tour's own floor: frontend/src/lib/demo/tours/controller.ts never opens
 *  a tour below 1024 px, so a narrower window gets the start page. */
const TOUR_FLOOR = 1024;
const DEMO_DOORS = ['the nav', 'the console figure'];
const HERO_ACTIONS = ['Install kew', 'How a change is proven →'];
const NAV_WITH_DEMO = ['Docs', 'How a change is proven', 'Demo', 'GitHub'];

const squash = (text) => text.replace(/\s+/g, ' ').trim();

/** Every link on the page to the demo, by where it sits, with its href as the
 *  HTML holds it now. */
async function demoLinks(page) {
  return page.evaluate(() =>
    [...document.querySelectorAll('a[href^="https://demo.kew.dev"]')].map((a) => ({
      href: a.getAttribute('href'),
      where: a.closest('.hero-stage nav')
        ? 'the nav'
        : a.closest('.console-figure figcaption')
          ? 'the console figure'
          : 'somewhere else'
    }))
  );
}

/** The link from each of the two places, and from nowhere else. */
function demoDoorProblems(links) {
  const problems = [];
  for (const where of DEMO_DOORS) {
    const n = links.filter((l) => l.where === where).length;
    if (n !== 1) problems.push(`${where} has ${n} demo links, not one`);
  }
  const stray = links.filter((l) => !DEMO_DOORS.includes(l.where)).length;
  if (stray > 0) problems.push(`${stray} demo links sit outside the two places`);
  return problems;
}

/** The hero's action row reads Install kew (filled, to #install) and How a
 *  change is proven (a ghost text link, to #loop), in that order; the demo's
 *  button sits under the console frame instead. The text link is seen as the
 *  nav links are (`sightOf`), measured from the top of the page. */
async function heroOffersTwoActions(page) {
  await page.evaluate(() => window.scrollTo({ top: 0, left: 0, behavior: 'instant' }));
  const found = await page.evaluate(() => {
    const row = document.querySelector('.hero-stage .ctas');
    if (!row) return null;
    const clear = (c) => c === 'transparent' || /rgba\(.*,\s*0\)$/.test(c);
    return [...row.querySelectorAll('a, button')].map((el) => {
      const style = getComputedStyle(el);
      return {
        text: el.textContent,
        href: el.getAttribute('href'),
        filled: !clear(style.backgroundColor),
        outlined:
          clear(style.backgroundColor) && style.borderTopStyle !== 'none' && !clear(style.borderTopColor)
      };
    });
  });
  if (found === null) return 'the hero has no action row';
  const problems = [];
  const texts = found.map((a) => squash(a.text));
  if (texts.join(' · ') !== HERO_ACTIONS.join(' · ')) {
    problems.push(`the action row reads "${texts.join(' · ')}"`);
  } else {
    const [install, proven] = found;
    if (install.href !== '#install') problems.push(`Install kew links to "${install.href}"`);
    if (!install.filled) problems.push('Install kew is not filled');
    if (proven.href !== '#loop') problems.push(`How a change is proven links to "${proven.href}"`);
    if (proven.filled || proven.outlined) problems.push('How a change is proven is not a text link');
    const link = page.locator('.hero-stage .ctas a[href="#loop"]').first();
    problems.push(...sightProblems('How a change is proven', await link.evaluate(sightOf, true)));
  }
  return problems.length === 0 ? null : problems.join('; ');
}

/** Once the page's script has run, every demo link opens the needs-you tour
 *  from the tour's 1024 px floor and the start page below it. */
async function demoLinksFollowWidth(page, size) {
  const want = size.width >= TOUR_FLOOR ? DEMO_TOUR_URL : DEMO_URL;
  const links = await demoLinks(page);
  const problems = demoDoorProblems(links);
  for (const link of links) {
    if (link.href !== want) problems.push(`${link.where} links to "${link.href}", not "${want}"`);
  }
  return problems.length === 0 ? null : problems.join('; ');
}

/** With script off, a desktop window still gets the start page from every
 *  demo link, as the HTML ships: never a tour URL the page did not choose. */
async function demoLinksWithoutScript(page, size) {
  const context = await page.context().browser().newContext({
    viewport: size,
    javaScriptEnabled: false
  });
  try {
    const bare = await context.newPage();
    await bare.goto(page.url(), { waitUntil: 'load' });
    const links = await demoLinks(bare);
    const problems = demoDoorProblems(links);
    for (const link of links) {
      if (link.href !== DEMO_URL) problems.push(`${link.where} links to "${link.href}"`);
    }
    return problems.length === 0 ? null : problems.join('; ');
  } finally {
    await context.close();
  }
}

/** The nav reads Docs · How it works · Demo · GitHub, and Demo is a 44 px tap
 *  target to the demo like the others. */
async function navShowsDemo(page) {
  const texts = await page.evaluate(() =>
    [...document.querySelectorAll('.hero-stage nav a')].map((a) =>
      a.textContent.replace(/[↗→]/g, '').trim()
    )
  );
  const problems = [];
  if (texts.join(' · ') !== NAV_WITH_DEMO.join(' · ')) {
    problems.push(`the nav reads "${texts.join(' · ')}"`);
  }
  const links = await demoLinks(page);
  if (!links.some((l) => l.where === 'the nav')) problems.push('the nav has no demo link');
  const why = await navTapTargets(page, ['Demo']);
  if (why !== null) problems.push(why);
  return problems.length === 0 ? null : problems.join('; ');
}

/** The hero's copy and rule are complete at rest: they hold no button, so
 *  nothing in them waits on a click, and no approval toy remains from the
 *  fleet card they replaced. (The console figure under them has its stops.) */
async function heroHasNoButtons(page) {
  const count = await page.evaluate(
    () => document.querySelectorAll('.hero-stage .hero button').length
  );
  return count === 0 ? null : `the hero holds ${count} button(s)`;
}

/**
 * With script off (design review c9, C1 and C8), the prerendered page holds
 * what the script would compute and shows what does not need it: every
 * anchor shows its computed sha-256, prev binding and verdict, with a "chain
 * intact" status and a non-zero link count; the console figure draws the
 * live frame alone (the phone capture below 640 px) and no rail (the rail
 * needs the script); and the per-model fold is in the page. Each is seen by a user, not merely present
 * in the HTML.
 */
async function noScriptShowsComputedContent(page, size) {
  const context = await page.context().browser().newContext({
    viewport: size,
    javaScriptEnabled: false
  });
  try {
    const bare = await context.newPage();
    await bare.goto(page.url(), { waitUntil: 'load' });
    const found = await bare.evaluate(() => {
      const shown = (el) => {
        const box = el.getBoundingClientRect();
        return box.width > 0 && box.height > 0 && getComputedStyle(el).visibility === 'visible';
      };
      const text = (el) => (el ? el.textContent.replace(/\s+/g, ' ').trim() : '');
      const anchors = [...document.querySelectorAll('#evidence .anchor')].map((a) => ({
        hashes: [...a.querySelectorAll('.a-hash')].map(text),
        shown: shown(a)
      }));
      const status = text(document.querySelector('#evidence .v-state'));
      const frames = [...document.querySelectorAll('.console-figure .shots img')]
        .filter((img) => getComputedStyle(img).display !== 'none')
        .map((img) => img.getAttribute('src'));
      const stops = document.querySelectorAll('.console-figure button').length;
      const fold = document.querySelector('#evidence details.models summary');
      return { anchors, status, frames, stops, fold: fold ? shown(fold) : false };
    });
    const problems = [];
    if (found.anchors.length === 0) problems.push('the evidence section shows no anchors');
    found.anchors.forEach((a, i) => {
      if (!a.shown) problems.push(`anchor ${i + 1} is not shown`);
      if (a.hashes.length === 0 || a.hashes.some((h) => !/^[0-9a-f]{10}…[0-9a-f]{6}$/.test(h))) {
        problems.push(`anchor ${i + 1} shows hashes ${JSON.stringify(a.hashes)}`);
      }
    });
    if (!/^chain intact [1-9]\d* hashes, [1-9]\d* links? verified at build$/.test(found.status)) {
      problems.push(`the anchors' status reads "${found.status}"`);
    }
    const want = `${FRAME_DIR}${size.width < 640 ? PHONE.file : FRAMES[DEFAULT_FRAME].file}`;
    if (found.frames.length !== 1 || found.frames[0] !== want) {
      problems.push(`the figure draws ${JSON.stringify(found.frames)}, not ${want} alone`);
    }
    if (found.stops !== 0) problems.push(`the figure draws ${found.stops} stops without script`);
    if (!found.fold) problems.push('the per-model fold is not shown');
    return problems.length === 0 ? null : problems.join('; ');
  } finally {
    await context.close();
  }
}

/**
 * With script on, the anchors never claim a check the browser did not run
 * (design review c9, A2 items 8, 9 and 11): after mount the status says the
 * hashes were verified in this browser; altering the first anchor breaks the
 * chain; altering the last anchor, which no shown link binds, breaks it too,
 * since each anchor's own hash is checked against its record; restoring
 * returns it to intact. The drawn link the first anchor's hash feeds turns
 * red with a cross while it is altered, and green again once restored.
 */
async function anchorsVerifyInBrowser(page) {
  const status = () =>
    page.evaluate(() =>
      (document.querySelector('#evidence .v-state')?.textContent ?? '').replace(/\s+/g, ' ').trim()
    );
  const settle = (want) =>
    page
      .waitForFunction(
        (re) =>
          new RegExp(re).test(
            (document.querySelector('#evidence .v-state')?.textContent ?? '').replace(/\s+/g, ' ').trim()
          ),
        want.source,
        { timeout: 10000 }
      )
      .then(() => null)
      .catch(async () => `the status reads "${await status()}"`);
  const intact = /^chain intact \d+ hashes, \d+ links? verified in your browser$/;
  // The status leads with its mark, a cross, before the words.
  const broken = /^×?\s?chain broken /;
  const problems = [];
  const first = await settle(intact);
  if (first) problems.push(`after mount ${first}`);
  const alters = page.locator('#evidence .anchor .a-btn');
  const count = await alters.count();
  if (count < 2) return `the anchors have ${count} alter controls`;
  for (const [name, index] of [
    ['the first anchor', 0],
    ['the last anchor', count - 1]
  ]) {
    await alters.nth(index).scrollIntoViewIfNeeded();
    await alters.nth(index).click();
    const why = await settle(broken);
    if (why) problems.push(`altering ${name}: ${why}`);
    // Altering the first anchor breaks the link the second binds; altering
    // the last breaks its own hash and no link.
    const cut = index === 0 ? ['cut', 'ok'] : ['ok', 'ok'];
    await page.waitForTimeout(100);
    const drawn = await wiresDrawn(page);
    const states = drawn?.wires.map((w) => w.state) ?? [];
    if (JSON.stringify(states) !== JSON.stringify(cut)) problems.push(`altering ${name}, the links read ${JSON.stringify(states)}`);
    if ((drawn?.crosses ?? 0) !== cut.filter((c) => c === 'cut').length) {
      problems.push(`altering ${name}, ${drawn?.crosses ?? 0} links are crossed out`);
    }
    await alters.nth(index).click();
    const back = await settle(intact);
    if (back) problems.push(`restoring ${name}: ${back}`);
    await page.waitForTimeout(100);
    const mended = (await wiresDrawn(page))?.wires.map((w) => w.state) ?? [];
    if (JSON.stringify(mended) !== JSON.stringify(['ok', 'ok'])) problems.push(`restoring ${name}, the links read ${JSON.stringify(mended)}`);
  }
  return problems.length === 0 ? null : problems.join('; ');
}

/** A deep link lands on its target: loaded at /#loop or /#install, once the
 *  page has drawn the console figure's stops above it, the section's top is
 *  at the top of the viewport (design review c9, A2 item 12), or, for the
 *  last section, which can be shorter than the window, the page is scrolled
 *  to its end with the whole section in view. A malformed fragment (/#%E0,
 *  which decodeURIComponent refuses) throws no page error. */
async function deepLinkLandsOnTarget(page, size) {
  const context = await page.context().browser().newContext({ viewport: size, reducedMotion: 'reduce' });
  const problems = [];
  try {
    const errors = [];
    for (const id of ['loop', 'install']) {
      const linked = await context.newPage();
      linked.on('pageerror', (error) => errors.push(String(error)));
      await linked.goto(`${new URL(`#${id}`, page.url())}`, { waitUntil: 'networkidle' });
      await linked.waitForTimeout(500);
      const found = await linked.evaluate((target) => {
        const el = document.getElementById(target);
        if (!el) return null;
        const root = document.documentElement;
        return {
          top: el.getBoundingClientRect().top,
          atEnd: Math.abs(window.scrollY - (root.scrollHeight - root.clientHeight)) <= 1,
          inView: el.getBoundingClientRect().top >= -1 && el.getBoundingClientRect().bottom <= root.clientHeight + 1
        };
      }, id);
      if (found === null) problems.push(`the page has no #${id}`);
      else if (Math.abs(found.top) > 2 && !(found.atEnd && found.inView)) {
        problems.push(`#${id} sits ${Math.round(found.top)} px from the top of the viewport`);
      }
      await linked.close();
    }
    const bad = await context.newPage();
    bad.on('pageerror', (error) => errors.push(`/#%E0: ${String(error)}`));
    await bad.goto(`${new URL('/', page.url())}#%E0`, { waitUntil: 'networkidle' });
    await bad.waitForTimeout(500);
    for (const error of errors) problems.push(`page error ${error}`);
  } finally {
    await context.close();
  }
  return problems.length === 0 ? null : problems.join('; ');
}

// --- the console figure, the bars, the fold, the strip and the route ------

/** The rail's stops, and every frame image, as drawn now. */
async function figureState(page) {
  return page.evaluate(() => {
    const figure = document.querySelector('.console-figure');
    if (!figure) return null;
    const shown = (el) => {
      const box = el.getBoundingClientRect();
      return box.width > 0 && box.height > 0 && getComputedStyle(el).display !== 'none';
    };
    const rail = figure.querySelector('.rail');
    const railBox = rail?.getBoundingClientRect();
    return {
      height: figure.getBoundingClientRect().height,
      // The next section's top, from the top of the document.
      after: (document.getElementById('evidence')?.getBoundingClientRect().top ?? 0) + window.scrollY,
      rail: rail
        ? { shown: shown(rail), left: railBox.left, width: railBox.width }
        : null,
      frame: figure.querySelector('.frame').getBoundingClientRect(),
      stops: [...figure.querySelectorAll('button.stop')].map((b) => {
        const box = b.getBoundingClientRect();
        const dot = b.querySelector('.dot');
        const dotBox = dot?.getBoundingClientRect();
        const label = b.querySelector('.label');
        const labelBox = label?.getBoundingClientRect();
        return {
          text: b.textContent.replace(/\s+/g, ' ').trim(),
          pressed: b.getAttribute('aria-pressed'),
          shown: shown(b),
          height: box.height,
          width: box.width,
          dotX: dotBox ? dotBox.left + dotBox.width / 2 : null,
          dotFill: dot ? getComputedStyle(dot).backgroundColor : null,
          color: label ? getComputedStyle(label).color : null,
          label: labelBox
            ? { left: labelBox.left, right: labelBox.right, top: labelBox.top, bottom: labelBox.bottom }
            : null
        };
      }),
      images: [...figure.querySelectorAll('.shots img')].map((img) => ({
        src: img.getAttribute('src'),
        width: img.getAttribute('width'),
        height: img.getAttribute('height'),
        loading: img.getAttribute('loading'),
        decoding: img.getAttribute('decoding'),
        alt: img.getAttribute('alt') ?? '',
        shown: shown(img)
      })),
      cyan: getComputedStyle(document.documentElement).getPropertyValue('--accent-2').trim()
    };
  });
}

/** The one frame image drawn, as its file name, or why there is not one. */
function shownFrame(state) {
  const shown = state.images.filter((img) => img.shown);
  if (shown.length !== 1) return { why: `${shown.length} frames are drawn, not one` };
  return { file: shown[0].src.split('/').pop() };
}

/** CSS colour as rgb() for comparing a token with a computed colour. */
async function rgbOf(page, css) {
  return page.evaluate((value) => {
    const probe = document.createElement('span');
    probe.style.color = value;
    document.body.append(probe);
    const out = getComputedStyle(probe).color;
    probe.remove();
    return out;
  }, css);
}

/** What a stop reads: its time and its moment. */
const stopLabel = (frame) => `${frame.time} · ${frame.moment}`;

/** Every image the figure holds, in order: the desktop frames, then the phone capture. */
const FIGURE_IMAGES = [
  ...FRAMES.map((frame) => ({ file: frame.file, width: FRAME_WIDTH, height: FRAME_HEIGHT, alt: frame.alt })),
  { file: PHONE.file, width: PHONE.clip.width, height: PHONE.clip.height, alt: PHONE.alt }
];

/** The figure's images: sized, lazy, async, each with the alt console-frames.js writes for it. */
function figureImageProblems(state) {
  const problems = [];
  if (state.images.length !== FIGURE_IMAGES.length) problems.push(`the figure holds ${state.images.length} images`);
  state.images.forEach((img, i) => {
    const want = FIGURE_IMAGES[i];
    if (!want) return;
    if (img.src !== `${FRAME_DIR}${want.file}`) problems.push(`image ${i + 1} is "${img.src}"`);
    if (img.width !== String(want.width) || img.height !== String(want.height)) {
      problems.push(`image ${i + 1} is ${img.width}x${img.height}`);
    }
    if (img.loading !== 'lazy' || img.decoding !== 'async') problems.push(`image ${i + 1} is not lazy and async`);
    if (img.alt !== want.alt) problems.push(`image ${i + 1}'s alt reads "${img.alt}"`);
  });
  return problems;
}

/**
 * From 640 px the console figure draws the live frame at rest and a time
 * rail under it: one stop per frame, each a button at least 44 px on a side,
 * labelled with its time and its moment, its dot at its frame's replay clock
 * along the rail (the live frame at the right end), the labels clear of one
 * another and inside the frame. The default stop is pressed and cyan.
 * Pressing each stop draws its frame alone, presses it alone, and moves
 * nothing: the figure's height and the next section's top stay where they
 * were.
 */
async function consoleFigureSteps(page) {
  await page.locator('.console-figure').scrollIntoViewIfNeeded();
  const before = await figureState(page);
  if (before === null) return 'the page has no console figure';
  const problems = figureImageProblems(before);
  if (FRAMES[DEFAULT_FRAME].path !== '/epics/ledger-v2') problems.push('the default frame is not the live view');
  if (!before.rail?.shown) problems.push('the time rail is not drawn');
  const labels = before.stops.map((s) => s.text);
  const want = FRAMES.map(stopLabel);
  if (JSON.stringify(labels) !== JSON.stringify(want)) problems.push(`the stops read ${JSON.stringify(labels)}`);
  before.stops.forEach((stop, i) => {
    if (stop.height < TAP || stop.width < TAP) {
      problems.push(`the stop "${stop.text}" is ${stop.width.toFixed(1)}x${stop.height.toFixed(1)} px`);
    }
    if (before.rail && stop.dotX !== null) {
      const at = before.rail.left + (FRAMES[i].elapsed / END_MINUTES) * before.rail.width;
      if (Math.abs(stop.dotX - at) > 1) problems.push(`stop ${i + 1}'s dot sits ${Math.round(stop.dotX - at)} px from its time`);
    }
    if (stop.label && (stop.label.left < before.frame.left || stop.label.right > before.frame.right)) {
      problems.push(`stop ${i + 1}'s label runs outside the frame`);
    }
    for (const other of before.stops.slice(i + 1)) {
      const a = stop.label;
      const b = other.label;
      if (a && b && a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom) {
        problems.push(`"${stop.text}" and "${other.text}" overlap`);
      }
    }
  });
  const cyan = await rgbOf(page, before.cyan);
  const check = (state, index, when) => {
    const pressed = state.stops.map((s, i) => [s, i]).filter(([s]) => s.pressed === 'true');
    if (pressed.length !== 1 || pressed[0][1] !== index) {
      problems.push(`${when}, stop ${pressed.map(([, i]) => i + 1).join(',') || 'none'} is pressed, not ${index + 1}`);
    } else if (pressed[0][0].dotFill !== cyan || pressed[0][0].color !== cyan) {
      problems.push(
        `${when}, the pressed stop is not cyan (dot ${pressed[0][0].dotFill}, label ${pressed[0][0].color})`
      );
    }
    const drawn = shownFrame(state);
    if (drawn.why) problems.push(`${when}, ${drawn.why}`);
    else if (drawn.file !== FRAMES[index].file) problems.push(`${when}, ${drawn.file} is drawn`);
  };
  check(before, DEFAULT_FRAME, 'at rest');
  for (let i = 0; i < FRAMES.length; i++) {
    await page.locator('.console-figure button.stop').nth(i).click();
    // Past the stop's colour transition (1 ms with reduced motion), however
    // loaded the host: up to 2 s for the pressed stop to turn cyan.
    await page
      .waitForFunction(
        ([index, colour]) => {
          const stop = document.querySelectorAll('.console-figure button.stop')[index];
          const dot = stop?.querySelector('.dot');
          const label = stop?.querySelector('.label');
          return (
            stop?.getAttribute('aria-pressed') === 'true' &&
            dot &&
            label &&
            getComputedStyle(dot).backgroundColor === colour &&
            getComputedStyle(label).color === colour
          );
        },
        [i, cyan],
        { timeout: 2000 }
      )
      .catch(() => null);
    const after = await figureState(page);
    check(after, i, `after pressing stop ${i + 1}`);
    if (Math.abs(after.height - before.height) > 0.5 || Math.abs((after.after ?? 0) - (before.after ?? 0)) > 0.5) {
      problems.push(`pressing stop ${i + 1} moves the page by ${Math.round(after.height - before.height)} px`);
    }
  }
  await page.locator('.console-figure button.stop').nth(DEFAULT_FRAME).click();
  await page.evaluate(() => window.scrollTo({ top: 0, left: 0, behavior: 'instant' }));
  return problems.length === 0 ? null : [...new Set(problems)].join('; ');
}

/**
 * Below 640 px the figure draws one image, the phone capture of the live
 * view, and no rail; the desktop frames are not drawn and none of them is
 * fetched. From 640 px the phone capture is neither drawn nor fetched.
 */
async function figureFitsTheWidth(page, size) {
  await page.locator('.console-figure').scrollIntoViewIfNeeded();
  await page.waitForTimeout(300);
  const state = await figureState(page);
  if (state === null) return 'the page has no console figure';
  const problems = figureImageProblems(state);
  const fetched = await page.evaluate(() =>
    performance
      .getEntriesByType('resource')
      .map((entry) => new URL(entry.name).pathname)
      .filter((path) => path.startsWith('/console/'))
      .map((path) => path.split('/').pop())
  );
  const drawn = shownFrame(state);
  const phone = size.width < 640;
  const want = phone ? PHONE.file : FRAMES[DEFAULT_FRAME].file;
  if (drawn.why) problems.push(drawn.why);
  else if (drawn.file !== want) problems.push(`${drawn.file} is drawn, not ${want}`);
  const railShown = Boolean(state.rail?.shown) || state.stops.some((s) => s.shown);
  if (phone && railShown) problems.push('the rail is drawn on a phone');
  if (!phone && !railShown) problems.push('the rail is not drawn');
  const unwanted = phone ? FRAMES.map((f) => f.file) : [PHONE.file];
  for (const file of fetched.filter((f) => unwanted.includes(f))) problems.push(`${file} was fetched`);
  if (!fetched.includes(want)) problems.push(`${want} was not fetched`);
  await page.evaluate(() => window.scrollTo({ top: 0, left: 0, behavior: 'instant' }));
  return problems.length === 0 ? null : problems.join('; ');
}

/**
 * From 1000 px, where the strip is one row, the loop shows the live view's
 * "needs you" block under stage 05: its image carries the alt and the size
 * console-frames.js gives it, and the file is that size at its scale (so the
 * picture is the whole block that was measured, not a cut one); its frame
 * starts at stage 05's left edge, and the strip stays one row. Narrower it is
 * not drawn and not fetched.
 */
async function judgmentUnderStageFive(page, size) {
  await page.locator('#loop').scrollIntoViewIfNeeded();
  await page.waitForTimeout(300);
  if (size.width >= 1000) {
    await page
      .waitForFunction(() => document.querySelector('#loop .judgment img')?.complete, null, { timeout: 5000 })
      .catch(() => null);
  }
  const found = await page.evaluate(() => {
    const crop = document.querySelector('#loop .judgment');
    const img = crop?.querySelector('img');
    const five = document.querySelectorAll('#loop .stages > li')[4];
    const tops = new Set([...document.querySelectorAll('#loop .stages > li')].map((li) => Math.round(li.getBoundingClientRect().top)));
    return {
      shown: crop ? getComputedStyle(crop).display !== 'none' && crop.getBoundingClientRect().height > 0 : false,
      alt: img?.getAttribute('alt') ?? null,
      width: img?.getAttribute('width') ?? null,
      height: img?.getAttribute('height') ?? null,
      natural: img ? [img.naturalWidth, img.naturalHeight] : null,
      left: crop?.querySelector('.j-frame')?.getBoundingClientRect().left ?? null,
      fiveLeft: five?.getBoundingClientRect().left ?? null,
      rows: tops.size,
      fetched: performance
        .getEntriesByType('resource')
        .some((entry) => new URL(entry.name).pathname.endsWith('/needs-you.png'))
    };
  });
  const problems = [];
  if (size.width >= 1000) {
    if (!found.shown) problems.push('the needs-you crop is not drawn');
    if (found.alt !== JUDGMENT.alt) problems.push(`its alt reads "${found.alt}"`);
    if (found.width !== String(JUDGMENT.width) || found.height !== String(JUDGMENT.height)) {
      problems.push(`the crop is ${found.width}x${found.height}, not ${JUDGMENT.width}x${JUDGMENT.height}`);
    }
    const natural = [JUDGMENT.width * JUDGMENT.scale, JUDGMENT.height * JUDGMENT.scale];
    if (JSON.stringify(found.natural) !== JSON.stringify(natural)) {
      problems.push(`needs-you.png is ${found.natural?.join('x')}, not ${natural.join('x')}`);
    }
    if (found.left === null || Math.abs(found.left - found.fiveLeft) > 2) {
      problems.push(`the crop starts at ${Math.round(found.left)} px, stage 05 at ${Math.round(found.fiveLeft)} px`);
    }
    if (found.rows !== 1) problems.push(`the strip runs to ${found.rows} rows`);
  } else {
    if (found.shown) problems.push(`the needs-you crop is drawn at ${size.width} px`);
    if (found.fetched) problems.push(`the needs-you crop was fetched at ${size.width} px`);
  }
  await page.evaluate(() => window.scrollTo({ top: 0, left: 0, behavior: 'instant' }));
  return problems.length === 0 ? null : problems.join('; ');
}

/** "1106" as the page writes counts: "1,106". */
const count = (n) => n.toLocaleString('en-US');
/** Dollars as the page writes them: "$1,412.67". */
const dollars = (n) => `$${n.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

/** Each bar's accessible name and its parts' widths, against the parts'
 *  sum (the spend bar's 1px gaps between parts are not a part). */
async function barsDrawn(page) {
  return page.evaluate(() =>
    Object.fromEntries(
      [...document.querySelectorAll('#evidence [data-bar]')].map((bar) => {
        const whole = [...bar.children].reduce((sum, part) => sum + part.getBoundingClientRect().width, 0);
        const gap = parseFloat(getComputedStyle(bar).columnGap) || 0;
        return [
          bar.dataset.bar,
          {
            role: bar.getAttribute('role'),
            label: bar.getAttribute('aria-label'),
            parts: [...bar.children].map((part) => part.getBoundingClientRect().width / whole),
            // What the parts and the gaps between them fill, and the box they fill.
            filled: whole + gap * Math.max(0, bar.children.length - 1),
            inner: bar.clientWidth
          }
        ];
      })
    )
  );
}

/**
 * The two bars state the data's own figures: the share bar's accessible name
 * reads the snapshot's counts and the agent share computed from them, and its
 * filled part is that share of the bar; the spend bar's name reads each run
 * kind's spend and share of the pack's run-kind total, in the pack's order,
 * and each part's width is that share. Both are images to assistive tech.
 * The figures are worked out here from repo-stats.json and pack-figures.js's
 * raw numbers, not read from the page's own strings.
 */
async function barsStateTheData(page) {
  const bars = await barsDrawn(page);
  const problems = [];
  const share = STATS.agentPrs / STATS.mergedPrs;
  const shareLabel = `${count(STATS.agentPrs)} of ${count(STATS.mergedPrs)} merged pull requests, ${(share * 100).toFixed(1)}%, were opened by an agent`;
  const total = RUN_KINDS.reduce((sum, r) => sum + r.cost, 0);
  if (Math.abs(total - PACK.totalSpend) > 0.005) problems.push(`the run kinds sum to ${total}, the pack's total is ${PACK.totalSpend}`);
  const spendLabel = `Spend by run kind: ${RUN_KINDS.map(
    (r) => `${r.kind.replaceAll('_', ' ')} ${dollars(r.cost)}, ${((r.cost / total) * 100).toFixed(1)}%`
  ).join('; ')}`;
  const want = {
    share: { label: shareLabel, parts: [share, 1 - share] },
    spend: { label: spendLabel, parts: RUN_KINDS.map((r) => r.cost / total) }
  };
  for (const [name, expected] of Object.entries(want)) {
    const bar = bars[name];
    if (!bar) {
      problems.push(`there is no ${name} bar`);
      continue;
    }
    if (bar.role !== 'img') problems.push(`the ${name} bar has role "${bar.role}"`);
    if (bar.label !== expected.label) problems.push(`the ${name} bar is named "${bar.label}", not "${expected.label}"`);
    if (bar.parts.length !== expected.parts.length) {
      problems.push(`the ${name} bar has ${bar.parts.length} parts`);
      continue;
    }
    if (Math.abs(bar.filled - bar.inner) > 1) {
      problems.push(`the ${name} bar's parts and gaps fill ${bar.filled.toFixed(1)} px of its ${bar.inner} px`);
    }
    bar.parts.forEach((part, i) => {
      if (Math.abs(part - expected.parts[i]) > 0.005) {
        problems.push(`the ${name} bar's part ${i + 1} is ${(part * 100).toFixed(1)}% wide, not ${(expected.parts[i] * 100).toFixed(1)}%`);
      }
    });
  }
  return problems.length === 0 ? null : problems.join('; ');
}

const FOLD_SUMMARY = 'Per-model figures and the method';
const FOLD_HEADS = ['Model', 'Runner', 'Runs', 'Opened', 'Merged', 'Spend', 'Per merged PR'];

/**
 * The per-model figures sit in a native <details>, folded at rest under a
 * 44 px summary that reads FOLD_SUMMARY; opened, it shows the table (a row
 * per scorecard model, the seven columns) and its three notes, and nothing
 * in it reaches past the viewport outside its own scroller.
 */
async function foldHoldsTheTable(page) {
  const summary = page.locator('#evidence details.models > summary');
  if ((await summary.count()) !== 1) return 'the evidence section has no fold';
  const problems = [];
  const closed = await page.evaluate(() => document.querySelector('#evidence details.models').open);
  if (closed) problems.push('the fold is open at rest');
  const text = squash((await summary.textContent()) ?? '');
  if (text !== FOLD_SUMMARY) problems.push(`the fold reads "${text}"`);
  const box = await summary.boundingBox();
  if (!box || box.height < TAP) problems.push(`the summary is ${box?.height ?? 0} px tall`);
  await summary.scrollIntoViewIfNeeded();
  await summary.click();
  const found = await page.evaluate(() => {
    const fold = document.querySelector('#evidence details.models');
    const shown = (el) => el.getBoundingClientRect().height > 0;
    const table = fold.querySelector('table');
    return {
      open: fold.open,
      tableShown: table ? shown(table) : false,
      heads: table ? [...table.querySelectorAll('thead th')].map((th) => th.textContent.trim()) : [],
      rows: table ? table.querySelectorAll('tbody tr').length : 0,
      notes: [...fold.querySelectorAll('.foot-note')].filter(shown).length
    };
  });
  if (!found.open || !found.tableShown) problems.push('the opened fold does not show its table');
  if (JSON.stringify(found.heads) !== JSON.stringify(FOLD_HEADS)) problems.push(`the table's heads read ${JSON.stringify(found.heads)}`);
  if (found.rows !== SCORECARD.length) problems.push(`the table has ${found.rows} rows, the scorecard ${SCORECARD.length}`);
  if (found.notes !== 3) problems.push(`the fold shows ${found.notes} notes`);
  const past = await nothingPastRightEdge(page);
  if (past) problems.push(`opened, ${past}`);
  await summary.click();
  return problems.length === 0 ? null : problems.join('; ');
}

const STAGE_TITLES = [
  'You scope the work.',
  'It dispatches into isolation.',
  'It works under a hard cap.',
  "A second agent attacks the first one's work.",
  'You keep the judgment.',
  'Every decision is attested.'
];
/** The person's stages, by index: 01 and 05. */
const YOURS = [0, 4];

/** How many columns the strip has at a width: six, three on a tablet, one on a phone. */
const stripColumns = (width) => (width >= 1000 ? 6 : width >= 600 ? 3 : 1);

/**
 * The loop's six stages are one strip: the titles in order, each with one
 * sentence under it; stages 01 and 05 labelled "· you" with the label and the
 * top rule in magenta, the other four in neither; laid out in six columns,
 * three on a tablet, one on a phone.
 */
async function stageStripReads(page, size) {
  const found = await page.evaluate(() =>
    [...document.querySelectorAll('#loop .stages > li')].map((li) => {
      const style = getComputedStyle(li);
      const n = li.querySelector('.n');
      return {
        n: n?.textContent.trim() ?? '',
        nColor: n ? getComputedStyle(n).color : '',
        rule: style.borderTopColor,
        title: li.querySelector('.title')?.textContent.trim() ?? '',
        line: li.querySelector('.line')?.textContent.trim() ?? '',
        left: Math.round(li.getBoundingClientRect().left),
        top: Math.round(li.getBoundingClientRect().top)
      };
    })
  );
  const magenta = await rgbOf(page, 'var(--accent)');
  const problems = [];
  if (JSON.stringify(found.map((s) => s.title)) !== JSON.stringify(STAGE_TITLES)) {
    problems.push(`the strip reads ${JSON.stringify(found.map((s) => s.title))}`);
  }
  found.forEach((stage, i) => {
    const yours = YOURS.includes(i);
    const label = `${String(i + 1).padStart(2, '0')}${yours ? ' · you' : ''}`;
    if (stage.n !== label) problems.push(`stage ${i + 1} is labelled "${stage.n}"`);
    if (yours !== (stage.nColor === magenta) || yours !== (stage.rule === magenta)) {
      problems.push(`stage ${i + 1} is ${yours ? 'not ' : ''}magenta`);
    }
    if (!stage.line) problems.push(`stage ${i + 1} has no sentence`);
  });
  const columns = new Set(found.map((s) => s.left)).size;
  if (columns !== stripColumns(size.width)) {
    problems.push(`the strip has ${columns} columns at ${size.width} px, not ${stripColumns(size.width)}`);
  }
  return problems.length === 0 ? null : problems.join('; ');
}

const ROUTE_TITLES = ['The agent’s checkout', 'Copy the files out', 'Rebuild the commit', 'Scan, then push'];

/**
 * The contained route is four boxes joined by three arrows: the agent's
 * checkout first, dashed and labelled Untrusted, the others solid; in one row
 * from 900 px and stacked below it; and the proof docs are linked under it.
 */
async function routeDiagramReads(page, size) {
  const found = await page.evaluate(() => {
    const route = document.querySelector('#loop .route');
    if (!route) return null;
    return {
      steps: [...route.querySelectorAll('.step')].map((step) => ({
        where: step.querySelector('.where')?.textContent.trim() ?? '',
        title: step.querySelector('strong')?.textContent.trim() ?? '',
        dashed: getComputedStyle(step).borderTopStyle === 'dashed',
        left: Math.round(step.getBoundingClientRect().left),
        top: Math.round(step.getBoundingClientRect().top)
      })),
      arrows: [...route.querySelectorAll('.arrow')].filter((a) => a.getBoundingClientRect().width > 0).length,
      link: route.querySelector('a.doclink')?.getAttribute('href') ?? null
    };
  });
  if (found === null) return 'the loop has no route diagram';
  const problems = [];
  if (JSON.stringify(found.steps.map((s) => s.title)) !== JSON.stringify(ROUTE_TITLES)) {
    problems.push(`the route reads ${JSON.stringify(found.steps.map((s) => s.title))}`);
  }
  found.steps.forEach((step, i) => {
    if ((i === 0) !== step.dashed) problems.push(`box ${i + 1} is ${step.dashed ? '' : 'not '}dashed`);
  });
  if (found.steps[0]?.where !== 'Untrusted') problems.push(`the first box is labelled "${found.steps[0]?.where}"`);
  if (found.arrows !== 3) problems.push(`the route draws ${found.arrows} arrows`);
  const row = size.width >= 900;
  const lefts = new Set(found.steps.map((s) => s.left)).size;
  const tops = new Set(found.steps.map((s) => s.top)).size;
  if (row && tops !== 1) problems.push(`at ${size.width} px the boxes sit on ${tops} rows`);
  if (!row && lefts !== 1) problems.push(`at ${size.width} px the boxes are not stacked`);
  if (found.link !== PROOF_DOCS) problems.push(`the route links "${found.link}"`);
  return problems.length === 0 ? null : problems.join('; ');
}

/**
 * Nothing on the page advances by itself: with motion allowed, the frame
 * drawn and the stop pressed are the same three seconds later, and once the
 * page has settled it asks for no animation frames.
 */
async function nothingAdvancesByItself(page) {
  await page.locator('.console-figure').scrollIntoViewIfNeeded();
  await page.waitForTimeout(500);
  const before = await figureState(page);
  const framesBefore = await page.evaluate(() => window.__frames ?? -1);
  await page.waitForTimeout(3000);
  const after = await figureState(page);
  const framesAfter = await page.evaluate(() => window.__frames ?? -1);
  if (before === null) return 'the page has no console figure';
  if (framesBefore < 0) return 'the frame counter was not installed';
  const problems = [];
  const a = shownFrame(before);
  const b = shownFrame(after);
  if (a.file !== b.file) problems.push(`the figure moved from ${a.file} to ${b.file} by itself`);
  const pressed = (state) => state.stops.findIndex((s) => s.pressed === 'true');
  if (pressed(before) !== pressed(after)) problems.push('the pressed stop changed by itself');
  const asked = framesAfter - framesBefore;
  if (asked > 2) problems.push(`the page asked for ${asked} animation frames in 3 s at rest`);
  return problems.length === 0 ? null : problems.join('; ');
}

/** Each drawn link between the anchor cards: its state, and where it runs
 *  against the cards it joins. */
async function wiresDrawn(page) {
  return page.evaluate(() => {
    const box = document.querySelector('#evidence .anchors');
    if (!box) return null;
    const origin = box.getBoundingClientRect();
    const cards = [...box.querySelectorAll('.anchor')].map((card) => card.getBoundingClientRect());
    return {
      stacked: cards.length > 1 && cards[1].top >= cards[0].bottom - 1,
      cardLeft: cards.length ? cards[0].left - origin.left : null,
      wires: [...box.querySelectorAll('svg [data-wire]')].map((path) => {
        const b = path.getBBox();
        // Stacked, a link's leftmost x is its lane, and its box's height the
        // stretch of that lane it runs down.
        return { state: path.dataset.wire, left: b.x, right: b.x + b.width, top: b.y, bottom: b.y + b.height };
      }),
      crosses: box.querySelectorAll('svg .cross').length,
      hidden: box.querySelector('svg')?.getAttribute('aria-hidden') ?? null
    };
  });
}

/**
 * The anchor chain is drawn as well as stated: one link between each pair
 * of cards, hidden from assistive tech, green while it verifies. In a row it
 * runs across the gap between the cards; stacked, it runs down the left
 * edge, left of the cards, each link in its own lane, so no two share an x
 * over a common stretch.
 */
async function chainIsDrawn(page) {
  const found = await wiresDrawn(page);
  if (found === null) return 'the evidence section has no anchors';
  const problems = [];
  const states = found.wires.map((w) => w.state);
  if (JSON.stringify(states) !== JSON.stringify(['ok', 'ok'])) problems.push(`the links read ${JSON.stringify(states)}`);
  if (found.crosses !== 0) problems.push(`${found.crosses} links are crossed out`);
  if (found.hidden !== 'true') problems.push('the drawing is not hidden from assistive tech');
  if (found.stacked) {
    for (const w of found.wires) {
      if (w.left >= found.cardLeft) problems.push('stacked, a link does not run down the left edge');
    }
    // Two links on one lane over a common stretch overlap, and the later
    // paints over the earlier: a broken link would be half covered in green.
    found.wires.forEach((a, i) => {
      for (const b of found.wires.slice(i + 1)) {
        const shared = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
        if (Math.abs(a.left - b.left) < 1 && shared > 0.5) {
          problems.push(`stacked, two links share the lane at x ${Math.round(a.left)} over ${Math.round(shared)} px`);
        }
      }
    });
  }
  return problems.length === 0 ? null : [...new Set(problems)].join('; ');
}

/**
 * Where the browser cannot hash (no Web Crypto, as on plain http to a LAN
 * address), the anchors say so and allow nothing: the status reads "your
 * browser cannot recompute the hashes here" beside the build's verdict, and
 * every alter control is disabled. Run with crypto.subtle taken away before
 * the page's own script loads.
 */
async function cannotRecomputeSaysSo(page, size) {
  const context = await page.context().browser().newContext({ viewport: size, reducedMotion: 'reduce' });
  try {
    await context.addInitScript(() => {
      Object.defineProperty(Crypto.prototype, 'subtle', { get: () => undefined, configurable: true });
    });
    const bare = await context.newPage();
    await bare.goto(page.url(), { waitUntil: 'networkidle' });
    await bare.waitForTimeout(300);
    const found = await bare.evaluate(() => ({
      status: (document.querySelector('#evidence .v-state')?.textContent ?? '').replace(/\s+/g, ' ').trim(),
      buttons: [...document.querySelectorAll('#evidence .anchor .a-btn')].map((b) => b.disabled)
    }));
    const problems = [];
    if (!/^chain intact \d+ hashes, \d+ links? verified at build your browser cannot recompute the hashes here$/.test(found.status)) {
      problems.push(`the status reads "${found.status}"`);
    }
    if (found.buttons.length === 0 || found.buttons.some((disabled) => !disabled)) {
      problems.push(`the alter controls are ${JSON.stringify(found.buttons)}, not all disabled`);
    }
    return problems.length === 0 ? null : problems.join('; ');
  } finally {
    await context.close();
  }
}

/**
 * When the browser's hashing fails while an anchor is altered, the anchors
 * go back to the build's rows: none of them altered, every control disabled
 * and none offering a restore, and the status says the browser cannot
 * recompute. Run with crypto.subtle.digest made to throw once the page asks.
 */
async function failedRecomputeRestoresTheBuild(page, size) {
  const context = await page.context().browser().newContext({ viewport: size, reducedMotion: 'reduce' });
  try {
    await context.addInitScript(() => {
      const subtle = globalThis.crypto.subtle;
      const digest = subtle.digest.bind(subtle);
      subtle.digest = (...args) =>
        window.__breakDigest ? Promise.reject(new Error('no digest here')) : digest(...args);
    });
    const bare = await context.newPage();
    await bare.goto(page.url(), { waitUntil: 'networkidle' });
    const status = () =>
      bare.evaluate(() => (document.querySelector('#evidence .v-state')?.textContent ?? '').replace(/\s+/g, ' ').trim());
    await bare.waitForFunction(() => /in your browser/.test(document.querySelector('#evidence .v-state')?.textContent ?? ''), null, { timeout: 10000 });
    const alter = bare.locator('#evidence .anchor .a-btn').first();
    await alter.scrollIntoViewIfNeeded();
    await alter.click();
    await bare.waitForFunction(() => /chain broken/.test(document.querySelector('#evidence .v-state')?.textContent ?? ''), null, { timeout: 10000 });
    await bare.evaluate(() => {
      window.__breakDigest = true;
    });
    await alter.click();
    await bare.waitForTimeout(300);
    const found = await bare.evaluate(() => ({
      edited: document.querySelectorAll('#evidence .anchor.edited').length,
      buttons: [...document.querySelectorAll('#evidence .anchor .a-btn')].map((b) => ({
        disabled: b.disabled,
        text: b.textContent.trim()
      }))
    }));
    const problems = [];
    const said = await status();
    if (!/your browser cannot recompute the hashes here/.test(said)) problems.push(`the status reads "${said}"`);
    if (found.edited !== 0) problems.push(`${found.edited} anchor(s) stay altered`);
    if (found.buttons.some((b) => !b.disabled || b.text !== 'alter')) {
      problems.push(`the controls read ${JSON.stringify(found.buttons)}`);
    }
    return problems.length === 0 ? null : problems.join('; ');
  } finally {
    await context.close();
  }
}

/**
 * The install block says it scrolls: while the command runs past its box,
 * the box fades at its right edge, and once scrolled to its end (or when
 * the command fits) the fade is gone. The box takes keyboard focus and is
 * named, so a keyboard can scroll it.
 */
async function installBlockFades(page) {
  const found = await page.evaluate(async () => {
    const box = document.querySelector('#install .cmdscroll');
    if (!box) return null;
    const mask = () => getComputedStyle(box).maskImage || getComputedStyle(box).webkitMaskImage;
    const overflows = box.scrollWidth > box.clientWidth + 1;
    const atStart = mask();
    box.scrollLeft = box.scrollWidth;
    box.dispatchEvent(new Event('scroll'));
    await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    const atEnd = mask();
    box.scrollLeft = 0;
    box.dispatchEvent(new Event('scroll'));
    return {
      overflows,
      atStart,
      atEnd,
      tabindex: box.getAttribute('tabindex'),
      label: box.getAttribute('aria-label')
    };
  });
  if (found === null) return 'the install section has no command box';
  const problems = [];
  const faded = (mask) => Boolean(mask) && mask !== 'none';
  if (found.overflows && !faded(found.atStart)) problems.push('the command runs past its box and the box does not fade');
  if (!found.overflows && faded(found.atStart)) problems.push('the command fits and the box still fades');
  if (faded(found.atEnd)) problems.push('scrolled to its end, the box still fades');
  if (found.tabindex !== '0') problems.push(`the box has tabindex ${found.tabindex}`);
  if (!found.label) problems.push('the box has no accessible name');
  return problems.length === 0 ? null : problems.join('; ');
}

/**
 * A link that leaves the site says so: Demo and GitHub in the nav, GitHub in
 * the footer, and every other link to another origin anywhere on the page
 * carry "↗", drawn at every width. In the footer no
 * separator dot ends a line: each sits on the line of the item after it.
 */
async function awayLinksCarryTheArrow(page) {
  const found = await page.evaluate(() => {
    const drawn = (el) => el && el.getBoundingClientRect().width > 0 && getComputedStyle(el).display !== 'none';
    const arrow = (a) => {
      if (!a || !a.textContent.includes('↗')) return false;
      const away = a.querySelector('.away');
      return away ? drawn(away) : true;
    };
    const nav = [...document.querySelectorAll('.hero-stage nav a')];
    const byText = (name) => nav.find((a) => a.textContent.replace(/[↗→]/g, '').trim() === name);
    const footerGitHub = [...document.querySelectorAll('footer a')].find((a) => a.textContent.includes('GitHub'));
    const dots = [...document.querySelectorAll('footer .sep')].map((sep) => {
      const next = sep.nextElementSibling;
      return next ? Math.abs(sep.getBoundingClientRect().top + sep.getBoundingClientRect().height / 2 - (next.getBoundingClientRect().top + next.getBoundingClientRect().height / 2)) < 4 : false;
    });
    // Every other link to another site, wherever it sits.
    const away = [...document.querySelectorAll('a[href]')]
      .filter((a) => {
        const url = new URL(a.getAttribute('href'), location.href);
        return /^https?:$/.test(url.protocol) && url.origin !== location.origin;
      })
      .filter((a) => !arrow(a))
      .map((a) => a.textContent.replace(/\s+/g, ' ').trim());
    return {
      demo: arrow(byText('Demo')),
      github: arrow(byText('GitHub')),
      footer: arrow(footerGitHub),
      away,
      dots
    };
  });
  const problems = [];
  if (!found.demo) problems.push('Demo in the nav has no arrow');
  if (!found.github) problems.push('GitHub in the nav has no arrow');
  if (!found.footer) problems.push('GitHub in the footer has no arrow');
  for (const text of found.away) problems.push(`"${text}" leaves the site without an arrow`);
  if (found.dots.length === 0 || found.dots.some((ok) => !ok)) problems.push('a footer dot ends a line');
  return problems.length === 0 ? null : problems.join('; ');
}

const EVERY = SIZES.map((size) => size.width);
const TOUR_EDGE = TOUR_EDGE_SIZES.map((size) => size.width);

/** The widths the figure, the bars, the fold, the strip and the route are checked at. */
const FIGURE = [390, 1440, LAPTOP.width];

/** Each check by name, the widths it runs at, and the check. */
const CHECKS = [
  ['no sideways scroll, and html and body do not clip overflow-x', [...EVERY, LAPTOP.width], noSidewaysScroll],
  ['no element ends past the right edge outside a sideways scroller', [...EVERY, LAPTOP.width], nothingPastRightEdge],
  ['no element starts past the left edge outside a sideways scroller', EVERY, nothingPastLeftEdge],
  [
    'nothing is cut off by overflow hidden or clip, save the intended ellipses',
    [...EVERY, LAPTOP.width],
    nothingClippedInside
  ],
  ['the bar and the hero fill the shell width at 1440 px', [1440], barAndHeroFillShell],
  [
    'the nav shows Docs, How a change is proven and GitHub at every width with 44 px tap targets',
    EVERY,
    navShowsWithTapTargets
  ],
  ['the nav holds all its links on one row at every width', EVERY, navOnOneRow],
  [
    'the headline holds "Offload the work." on one line from 1080 px',
    [1080, 1440],
    headlineHoldsFirstLine
  ],
  [
    'the proof block fits, states the rule, draws the always-on rows, folds the opt-in rows and links the docs',
    [...EVERY, LAPTOP.width],
    proofCardFits
  ],
  [
    'at 1366x650 the rule, the three rows, the folded row and both doc links are in the first viewport',
    [LAPTOP.width],
    heroCardInFirstViewport
  ],
  [
    'from 640 px the console figure draws the live frame and a rail of four 44 px stops at their times, and a stop shows its frame without moving the page',
    [1440, LAPTOP.width, 1080, 768],
    consoleFigureSteps
  ],
  [
    'below 640 px the figure draws the phone capture and no rail, and each width fetches only its own image',
    [...EVERY, LAPTOP.width],
    figureFitsTheWidth
  ],
  [
    'from 1000 px the needs-you crop hangs under stage 05 and the strip stays one row',
    [...EVERY, LAPTOP.width],
    judgmentUnderStageFive
  ],
  ['the anchor chain is drawn, green, across the gap or down the left edge', [390, 768, 1440], chainIsDrawn],
  [
    'without Web Crypto the anchors say the browser cannot recompute and every alter is disabled',
    [390, 1440],
    cannotRecomputeSaysSo
  ],
  [
    'a recompute that fails while an anchor is altered returns every row to the build',
    [1440],
    failedRecomputeRestoresTheBuild
  ],
  ['the install block fades at its right edge until scrolled to its end, and takes focus', EVERY, installBlockFades],
  ['links that leave the site carry the arrow, and no footer dot ends a line', EVERY, awayLinksCarryTheArrow],
  ['the share bar and the spend bar state the data in their names and widths', FIGURE, barsStateTheData],
  ['the per-model fold is folded at rest and opens on its table and notes', FIGURE, foldHoldsTheTable],
  [
    'the loop strip reads six stages, 01 and 05 in magenta, in 6, 3 or 1 columns',
    [...EVERY, LAPTOP.width],
    stageStripReads
  ],
  [
    'the contained route is four boxes and three arrows, the first dashed, in a row from 900 px',
    [...EVERY, LAPTOP.width],
    routeDiagramReads
  ],
  ['the install command is drawn on one line, never broken inside a token', EVERY, installOnOneLine],
  [
    'the install block runs the column width and scrolls its command sideways inside itself',
    EVERY,
    installScrollsInItsBlock
  ],
  ['every font the page uses decodes', EVERY, fontsDecode],
  ['Copy copies the install command exactly', [1440], copyCopiesInstall],
  ['small text meets WCAG AA contrast at 390 and 1440 px', [390, 1440], smallTextMeetsAA],
  ['the hero offers Install kew and How a change is proven', EVERY, heroOffersTwoActions],
  [
    'the demo link opens the needs-you tour on a desktop and the start page on a phone',
    [...EVERY, ...TOUR_EDGE],
    demoLinksFollowWidth
  ],
  ['without script every demo link opens the start page', [1440], demoLinksWithoutScript],
  ['the nav shows Demo at every width', EVERY, navShowsDemo],
  ['the hero holds no button, so it is complete at rest', EVERY, heroHasNoButtons],
  [
    'with script the anchors verify in the browser, break on the first and last anchor, and restore',
    [1440],
    anchorsVerifyInBrowser
  ],
  [
    'a link to /#loop or /#install lands on its section, and /#%E0 throws no page error',
    [390, 1440],
    deepLinkLandsOnTarget
  ],
  [
    'without script the anchors show computed hashes, the figure the live frame alone and no rail',
    [390, 1440],
    noScriptShowsComputedContent
  ],
  // Last, so it also sees any request the other checks set off.
  ['the page answers 200 and every request succeeds', [...EVERY, LAPTOP.width], everyRequestSucceeds]
];

// --- the run ---------------------------------------------------------------

function shotsDir() {
  const at = process.argv.indexOf('--shots');
  return at === -1 ? null : resolve(process.argv[at + 1] ?? 'browser-check-shots');
}

/** A page at one size, with every failed or erroring request it makes and
 *  every uncaught page error noted. Reduced motion unless `motion` is set. */
async function openPage(browser, origin, size, { motion = false } = {}) {
  const context = await browser.newContext({
    viewport: size,
    reducedMotion: motion ? 'no-preference' : 'reduce'
  });
  await context.grantPermissions(['clipboard-read', 'clipboard-write'], { origin });
  // Counts the animation frames the page asks for (nothingAdvancesByItself).
  await context.addInitScript(() => {
    const ask = window.requestAnimationFrame.bind(window);
    window.__frames = 0;
    window.requestAnimationFrame = (callback) => {
      window.__frames += 1;
      return ask(callback);
    };
  });
  const page = await context.newPage();
  const run = { status: null, failures: [], pageErrors: [] };
  // A thrown error can kill a component's reactivity while the page still
  // looks right (PLAYBOOK.md), so any one fails the run.
  page.on('pageerror', (error) => run.pageErrors.push(String(error)));
  page.on('requestfailed', (request) => {
    run.failures.push(`${request.url()} failed: ${request.failure()?.errorText ?? 'unknown'}`);
  });
  page.on('response', (response) => {
    if (response.status() >= 400) {
      run.failures.push(`${response.url()} answered ${response.status()}`);
    }
  });
  const response = await page.goto(origin, { waitUntil: 'networkidle' });
  run.status = response ? response.status() : null;
  await page.evaluate(() => document.fonts.ready);
  return { context, page, run };
}

async function main() {
  if (!existsSync(join(ROOT, 'index.html'))) {
    console.error(`browser-check: no build at ${ROOT}; run \`npm run build\` first`);
    return 2;
  }
  const executablePath = chromiumBinary();
  if (executablePath === null) {
    console.error(
      'browser-check: no Chromium found in $PLAYWRIGHT_BROWSERS_PATH or the Playwright cache; ' +
        'install one with `npx playwright-core install chromium-headless-shell`'
    );
    return 2;
  }
  const { chromium } = await import('playwright-core');
  const shots = shotsDir();
  if (shots) mkdirSync(shots, { recursive: true });
  const server = await serve();
  const origin = `http://127.0.0.1:${server.address().port}/`;
  const browser = await chromium.launch({ executablePath });
  let failed = 0;
  const report = (label, name, why) => {
    if (why === null) {
      console.log(`PASS ${label}  ${name}`);
    } else {
      failed++;
      console.log(`FAIL ${label}  ${name}: ${why}`);
    }
  };
  const pageErrors = (run) =>
    run.pageErrors.length === 0 ? null : run.pageErrors.slice(0, 3).join('; ');
  try {
    for (const size of [...SIZES, ...TOUR_EDGE_SIZES, LAPTOP]) {
      const { context, page, run } = await openPage(browser, origin, size);
      const label = size === LAPTOP ? `${size.width}x${size.height}` : `${size.width}px`;
      if (shots) {
        await page.screenshot({ path: join(shots, `front-${size.width}.png`), fullPage: true });
      }
      for (const [name, widths, check] of CHECKS) {
        if (!widths.includes(size.width)) continue;
        report(label, name, await check(page, size, run));
      }
      report(label, 'the page throws no uncaught error', pageErrors(run));
      await context.close();
    }

    // The one pass with motion allowed.
    const { context, page, run } = await openPage(browser, origin, MOTION, { motion: true });
    const label = `${MOTION.width}px, motion on`;
    report(label, 'no sideways scroll, and html and body do not clip overflow-x', await noSidewaysScroll(page));
    report(label, 'nothing on the page advances by itself', await nothingAdvancesByItself(page));
    report(label, 'the page answers 200 and every request succeeds', await everyRequestSucceeds(page, MOTION, run));
    report(label, 'the page throws no uncaught error', pageErrors(run));
    await context.close();
  } finally {
    await browser.close();
    server.close();
  }
  console.log(failed === 0 ? 'browser-check: all checks pass' : `browser-check: ${failed} failed`);
  return failed === 0 ? 0 : 1;
}

process.exitCode = await main();
