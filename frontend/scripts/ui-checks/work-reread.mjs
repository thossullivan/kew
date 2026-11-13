/**
 * #2916 — Work renders what its pulse-driven re-reads return.
 *
 * `hidden-tab.mjs` proves Work's re-reads RUN on the pulse (#2888): the list,
 * the selected item's pipeline and its evidence are each asked for again. It
 * cannot prove they are APPLIED. The fixture server answers the same reading
 * every time, so a page that re-read and threw the answer away — an unassigned
 * `await fetchWorkProjection()`, a reader whose result is dropped — would make
 * every request that check counts and leave the screen exactly as it was.
 *
 * Here the server's reading moves. Each of the three reads is served version A
 * (the committed fixture) until A is on screen, then version B, which changes
 * one value each place: the row's state word, a pipeline stage, an evidence
 * line. The page is never reloaded and nothing is clicked, so the only way B
 * reaches the screen is a pulse's re-read being taken. Each value is read from
 * the one element that shows it — the row's state word, the merge stage's
 * label, the verdict's summary — as exact text, and counts only where the
 * screen draws it. `hiddenFails` proves both in the browser: it hides each B
 * value with CSS, or moves it into a sibling, and watches its place read as
 * stale. `work-reread.test.ts`
 * is this check's own test, with the negative control: a page that sends every
 * re-read and drops what comes back fails it.
 *
 * One surface of `npm run ui-check`; `scripts/ui-check.mjs` runs it (#2570).
 */
// ui-check-owns: src/routes/(factory)/work/+page.svelte
// ui-check-owns: src/lib/work/reads.ts
// ui-check-owns: src/lib/work/Pipeline.svelte
// ui-check-owns: src/lib/work/EvidenceLedger.svelte
import { readFileSync } from 'node:fs';
import { FRONTEND } from '../ui-check-lib.mjs';

/** The item: selected, with a row, a pipeline and an evidence chain. */
export const ITEM = '2055';

/** The shell pulse's interval (`PULSE_INTERVAL_MS`). The first pulse after load
 * is the reading `load()` already took, so Work re-reads from the second on. */
const PULSE_MS = 7_000;

/** How long version B is given to reach the screen: two pulses and slack. */
export const REREAD_MS = 2 * PULSE_MS + 3_000;

/** How long version A is given to reach the screen on load. */
const LOAD_MS = 15_000;

const fixture = (name) =>
  JSON.parse(readFileSync(new URL(`fixtures/factory/${name}.json`, FRONTEND), 'utf8'));

/**
 * Version B of each read, made from the committed fixture (version A). The
 * item's merge decision is no longer outstanding, so its row reads DONE where
 * it read IN REVIEW; the merge stage is done; the review verdict is a later
 * round's (A's round 1 is already READY, #3461).
 */
export function versionB({ work, pipeline, evidence }) {
  const views = work.views.map((view) =>
    view.identity.item_id === ITEM
      ? { ...view, exception: { state: 'none', detail: '', blocking: false, dimension: '' } }
      : view
  );
  const lanes = pipeline.lanes.map((lane) => ({
    ...lane,
    stages: lane.stages.map((stage) =>
      stage.stage === 'merge'
        ? { ...stage, state: 'done', label: 'merged', detail: 'pull request merged' }
        : stage
    )
  }));
  const rows = evidence.rows.map((row) =>
    row.event === VERDICT
      ? { ...row, summary: 'ready with residuals', ts: '2026-10-03T14:47:50+00:00', state: 'attested' }
      : row
  );
  return {
    work: { ...work, views },
    pipeline: { ...pipeline, lanes },
    evidence: { ...evidence, rows, attested_count: rows.filter((r) => r.state === 'attested').length }
  };
}

/** Both versions of the three reads. */
export function versions() {
  const a = {
    work: fixture('dashboard-work'),
    pipeline: fixture(`work-${ITEM}-pipeline`),
    evidence: fixture(`work-${ITEM}-evidence`)
  };
  return { a, b: versionB(a) };
}

/**
 * What each version reads on screen, per place, exactly. `row` is the row's
 * state word, `stage` the merge stage's label, `line` the verdict's summary
 * (the ledger prints it as `event · summary`) — A's and B's differ in each, so
 * seeing B's is seeing B taken.
 */
const VERDICT = 'adversarial_review_verdict';
export const READS = {
  row: { what: 'the Work row', a: 'in review', b: 'done' },
  stage: { what: 'the pipeline’s merge stage', a: 'needs you', b: 'merged' },
  line: {
    what: 'the evidence ledger’s verdict line',
    a: `${VERDICT} · ready`,
    b: `${VERDICT} · ready with residuals`
  }
};

/** Where each read is served from. */
const PATHS = {
  work: '**/api/dashboard/work',
  pipeline: `**/api/work/${ITEM}/pipeline`,
  evidence: `**/api/work/${ITEM}/evidence`
};

/**
 * Serve the three reads from `page.route`: version A until `flip()`, then B.
 * `served` counts what each version answered, so a failure can say whether
 * the page asked for B at all.
 */
export async function serveVersions(page, { a, b } = versions()) {
  const state = { version: 'a', served: { a: 0, b: 0 } };
  for (const [key, glob] of Object.entries(PATHS)) {
    await page.route(glob, (route) => {
      state.served[state.version] += 1;
      const body = JSON.stringify((state.version === 'a' ? a : b)[key]);
      return route.fulfill({ status: 200, contentType: 'application/json', body });
    });
  }
  return {
    served: state.served,
    flip() {
      state.version = 'b';
    }
  };
}

/**
 * The three places, as the screen DRAWS them — runs in the page, so it closes
 * over nothing. Each place is one element, found by its own hook: the item
 * row's state word, the merge stage's label, the verdict row's summary. A
 * container's text would not do: B's words in a sibling (the merge stage's
 * detail, another evidence row) would read as the place's own.
 *
 * What the element reads is its text nodes the screen draws, joined: the
 * node's element passes `checkVisibility()` (no `display: none`, `visibility:
 * hidden` or zero opacity on it or above it), its text colour has alpha above
 * zero, and the glyphs have a box with width and height that no ancestor with
 * `overflow` other than `visible` clips away entirely. The element is scrolled
 * into view first, so a lane that scrolls sideways on a phone shows the cell
 * it holds; a box clipped to nothing stays clipped.
 *
 * `hide` is the check's control, not a reading: it puts `css` on the place's
 * element (`on: 'value'`) or its parent (`on: 'ancestor'`), and with `twin`
 * puts the value in a new sibling beside it, so the control can watch the
 * reading lose it. `unhide` takes every such style and sibling off again.
 * `kept` is whether the document `keep()` marked is still the one on screen.
 */
function look({ item, hide = null, unhide = false }) {
  const found = {
    // The state is the row's one StatusMark with a word; the diamond has none.
    row: document.querySelector(
      `aside[aria-label="Issues"] a[data-item="${item}"] [data-status-word]`
    ),
    stage: document.querySelector(
      '[aria-label="Pipeline stages"] [data-stage="merge"] [data-caption="primary"]'
    ),
    line: document.querySelector(
      'section[aria-label="Evidence ledger"] li[data-event="adversarial_review_verdict"] [data-summary]'
    )
  };
  const texts = (el) => {
    const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
    const nodes = [];
    for (let node = walker.nextNode(); node; node = walker.nextNode()) nodes.push(node);
    return nodes;
  };
  // `rgb(…)`, `rgba(…, a)`, `oklch(… / a)`, `color(srgb … / a)`: the alpha is
  // after the slash or the fourth comma, and 1 when neither is written.
  const alpha = (color) => {
    const inner = color.slice(color.indexOf('(') + 1, color.lastIndexOf(')'));
    const part = inner.includes('/') ? inner.split('/')[1] : inner.split(',')[3];
    if (part === undefined) return color === 'transparent' ? 0 : 1;
    const value = parseFloat(part);
    return part.trim().endsWith('%') ? value / 100 : value;
  };
  const unclipped = (box, node) => {
    let { left, right, top, bottom } = box;
    for (let el = node.parentElement; el; el = el.parentElement) {
      const style = getComputedStyle(el);
      const edge = el.getBoundingClientRect();
      if (style.overflowX !== 'visible') {
        left = Math.max(left, edge.left);
        right = Math.min(right, edge.right);
      }
      if (style.overflowY !== 'visible') {
        top = Math.max(top, edge.top);
        bottom = Math.min(bottom, edge.bottom);
      }
    }
    return right > left && bottom > top;
  };
  const opts = { opacityProperty: true, visibilityProperty: true };
  const range = document.createRange();
  const drawn = (node) => {
    const el = node.parentElement;
    if (!el?.checkVisibility(opts) || !(alpha(getComputedStyle(el).color) > 0)) return false;
    range.selectNodeContents(node);
    return [...range.getClientRects()].some(
      (box) => box.width > 0 && box.height > 0 && unclipped(box, node)
    );
  };
  const read = (el) => {
    if (!el) return '';
    el.scrollIntoView({ block: 'nearest', inline: 'nearest' });
    return texts(el)
      .filter(drawn)
      .map((node) => node.data)
      .join('')
      .replace(/\s+/g, ' ')
      .trim();
  };

  if (unhide) {
    for (const el of document.querySelectorAll('[data-kew-reread-twin]')) el.remove();
    for (const el of document.querySelectorAll('[data-kew-reread-style]')) {
      el.setAttribute('style', el.getAttribute('data-kew-reread-style'));
      el.removeAttribute('data-kew-reread-style');
    }
  }
  let hid = '';
  const el = hide && found[hide.place];
  const target = el && (hide.on === 'ancestor' ? el.parentElement : el);
  if (target && !target.hasAttribute('data-kew-reread-style')) {
    target.setAttribute('data-kew-reread-style', target.getAttribute('style') ?? '');
    target.setAttribute('style', `${target.getAttribute('style') ?? ''};${hide.css}`);
    hid = `${hide.on === 'ancestor' ? 'parent ' : ''}${target.tagName.toLowerCase()}`;
    if (hide.twin) {
      const twin = document.createElement('span');
      twin.setAttribute('data-kew-reread-twin', '');
      twin.textContent = hide.value;
      el.after(twin);
      hid += ' and a sibling holding it';
    }
  }
  return {
    row: read(found.row),
    stage: read(found.stage),
    line: read(found.line),
    kept: globalThis.__kewWorkReread === true,
    hid
  };
}

/** The three places as `look` reads them. A page caught mid-navigation reads
 * as nothing drawn. */
export function onScreen(page, extra = {}) {
  return page
    .evaluate(look, { item: ITEM, ...extra })
    .catch(() => ({ row: '', stage: '', line: '', kept: false, hid: '' }));
}

/**
 * Mark the document on screen. A reload would read version B through
 * `load()`, not through a pulse — the check repairing what it looks for — so
 * B only counts on the document that showed A.
 */
function keep(page) {
  return page.evaluate(() => {
    globalThis.__kewWorkReread = true;
  });
}

/** True once every place in `places` reads exactly `version`'s value, looked
 * at every 250ms. */
async function reads(page, places, version, ms, sleep) {
  let screen = await onScreen(page);
  const all = () => places.every((key) => screen[key] === READS[key][version]);
  for (let waited = 0; !all() && waited < ms; waited += 250) {
    await sleep(250);
    screen = await onScreen(page);
  }
  return screen;
}

/** The places in `places` where `screen` does not draw version B's value. */
const staleIn = (screen, places) => places.filter((key) => screen[key] !== READS[key].b);

/**
 * Version A on screen in each of `places`, then the flip, then version B there
 * by the pulse's re-reads alone. A must be seen first: a screen that showed B
 * from the start would pass without a re-read ever being taken.
 */
export async function pulseApplied(page, served, { sleep, expect, places = Object.keys(READS) }) {
  const before = await reads(page, places, 'a', LOAD_MS, sleep);
  for (const key of places) {
    const { what, a } = READS[key];
    expect(before[key] === a, `${what} never read "${a}" on load: "${before[key]}"`);
  }
  await keep(page);
  served.flip();
  const after = await reads(page, places, 'b', REREAD_MS, sleep);
  expect(after.kept, 'the page was reloaded after the flip: version B came from a fresh load');
  const stale = staleIn(after, places);
  expect(
    stale.length === 0,
    stale
      .map((key) => `${READS[key].what} still reads "${after[key]}", not "${READS[key].b}"`)
      .join('\n') +
      `\n${REREAD_MS / 1000}s after the server moved to version B ` +
      `(${served.served.b} version-B answer(s) served): the re-reads ran and were dropped`
  );
}

/**
 * The ways a page can keep a value in its DOM and off the screen, or put it
 * somewhere other than the place it names. `on` is which element the style
 * goes on: the value's own, or its parent. `twin` also puts the value in a
 * new sibling — B's words beside the place, the place itself gone.
 */
const CLIP =
  'display: block !important; overflow: hidden !important; height: 0 !important; ' +
  'min-height: 0 !important; max-height: 0 !important; padding: 0 !important; border: 0 !important';
export const HIDINGS = [
  { name: 'display: none', on: 'value', css: 'display: none !important' },
  { name: 'visibility: hidden', on: 'value', css: 'visibility: hidden !important' },
  { name: 'opacity: 0', on: 'value', css: 'opacity: 0 !important' },
  { name: 'font-size: 0', on: 'value', css: 'font-size: 0 !important' },
  { name: 'color: transparent', on: 'value', css: 'color: transparent !important' },
  { name: 'an ancestor clip', on: 'ancestor', css: CLIP },
  { name: 'only a sibling', on: 'value', css: 'display: none !important', twin: true }
];

/**
 * The control on the reading itself, once version B is on screen: hide B's
 * value in each place, each way `HIDINGS` names, and the place must read as
 * stale — the verdict `pulseApplied` gives a page that never drew B. Then the
 * style comes off and B must read again, so it was the hiding that lost it. A
 * reading that counted text the screen does not draw, or a neighbour's text,
 * would pass a blank place here, and fails this instead.
 */
export async function hiddenFails(page, { expect, places = Object.keys(READS) }) {
  for (const place of places) {
    const { what, b } = READS[place];
    for (const hiding of HIDINGS) {
      const { name } = hiding;
      const hidden = await onScreen(page, { hide: { place, value: b, ...hiding } });
      expect(hidden.hid !== '', `control: no element in ${what} holds "${b}" to hide`);
      expect(
        staleIn(hidden, [place]).length === 1,
        `control: ${what} still reads "${b}" under ${name} (${hidden.hid}): ` +
          'the check counts text the place does not draw'
      );
      const shown = await onScreen(page, { unhide: true });
      expect(
        staleIn(shown, [place]).length === 0,
        `control: ${what} does not read "${b}" again once ${name} is off: "${shown[place]}"`
      );
    }
  }
}

/** Open the evidence ledger, which starts closed (#2786 D16). */
async function openLedger(page) {
  const toggle = page.locator('button[data-panel="Evidence ledger"]');
  await toggle.waitFor({ state: 'visible', timeout: LOAD_MS });
  if ((await toggle.getAttribute('aria-expanded')) !== 'true') await toggle.click();
}

/**
 * Where each width finds the three places. At 1440 the list and the selected
 * item's detail are side by side, so one page holds all three. Below 1024 the
 * two panes take turns (#2281 D2): the row is on the list, the stage and the
 * line on the item's detail, so each is its own page — never a press from one
 * to the other, which would be a second way for a reading to arrive.
 */
export const VISITS = {
  wide: [{ visit: `/work?item=${ITEM}`, places: ['row', 'stage', 'line'] }],
  phone: [
    { visit: '/work', places: ['row'] },
    { visit: `/work?item=${ITEM}`, places: ['stage', 'line'] }
  ]
};

export async function checks({ check, visit, expect, sleep, sizes: { WIDTH, HEIGHT } }) {
  const SIZES = [
    { size: { width: WIDTH, height: HEIGHT }, visits: VISITS.wide },
    { size: { width: 390, height: 844 }, visits: VISITS.phone }
  ];
  for (const { size, visits } of SIZES) {
    for (const { visit: route, places } of visits) {
      const named = places.map((key) => READS[key].what).join(', ');
      let served;
      await check(`${route} at ${size.width}px draws what its pulse re-reads return: ${named}`, () =>
        visit(
          route,
          async (page, problems) => {
            if (places.includes('line')) await openLedger(page);
            await pulseApplied(page, served, { sleep, expect, places });
            await hiddenFails(page, { expect, places });
            expect(problems.length === 0, problems.join('\n'));
          },
          size,
          async (page) => {
            served = await serveVersions(page);
          }
        )
      );
    }
  }
}
