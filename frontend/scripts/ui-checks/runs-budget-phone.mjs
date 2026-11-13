/**
 * Runs and Budget at phone width: nothing clipped, nothing run together
 * (#3457, design review c9 D10–D13).
 *
 * At 390 the Runs readouts were three columns, so "SUCCESS RATE" ran into
 * "TOTAL COST" and the last column left the screen; the table head printed
 * "RUNRUNNER" and cut the cost column to "$0"; the status filters were 14px
 * words. Budget's spend readouts printed "TODAY71.2% of cap" with no gap and
 * cut "THIS WEEK" at the right edge, and its caps table needs 584px of fixed
 * track, so it stopped at "KE…".
 *
 * Each page is read on the fixture world at 390, 768 and 1440. Every visible
 * element in the page's pane must sit inside the pane and the window; every
 * readout's label, meta and figure inside its cell, a label and its meta
 * apart by at least 4px when they share a line; every table cell inside its
 * row and clear of its siblings, and unclipped (a run's title may end in an
 * ellipsis — it is the one cell that gives way); no two visible column heads
 * overlap; and at 390 every Runs filter is at least 32px tall. The pages'
 * gutter is 16px at 390 and 40px from 640 up, Scorecard's included. Scorecard
 * is read the same way since the fixture world serves it data (#3461): its
 * seven-column model table wraps below 1024, as Budget's caps table does.
 *
 * One surface of `npm run ui-check`; `scripts/ui-check.mjs` runs it (#2570).
 */
// ui-check-owns: src/routes/(factory)/runs/+page.*
// ui-check-owns: src/routes/(factory)/budget/+page.*
// ui-check-owns: src/routes/(factory)/scorecard/+page.*
// ui-check-owns: src/lib/ui/Readout.svelte

const PHONE = { width: 390, height: 844 };
const TABLET = { width: 768, height: 1024 };

/** Every fit problem on the page, as sentences; an empty list is a pass. */
function readFit({ readouts, rows, heads, filters, h1 }) {
  const problems = [];
  const vw = document.documentElement.clientWidth;
  const box = (el) => el.getBoundingClientRect();
  const shown = (el) => {
    const r = box(el);
    const s = getComputedStyle(el);
    return r.width > 1 && r.height > 1 && s.visibility !== 'hidden' && s.display !== 'none';
  };
  const name = (el) =>
    `${el.tagName.toLowerCase()} "${(el.textContent ?? '').replace(/\s+/g, ' ').trim().slice(0, 40)}"`;
  const within = (inner, outer, slack = 0.5) =>
    inner.left >= outer.left - slack && inner.right <= outer.right + slack;
  const overlap = (a, b) =>
    Math.min(a.right, b.right) - Math.max(a.left, b.left) > 0.5 &&
    Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top) > 0.5;
  const clipped = (el) => el.scrollWidth > el.clientWidth + 1;

  const heading = document.querySelector(h1);
  if (!heading) return { problems: [`no ${h1} on the page`], counts: {} };
  const pane = heading.closest('.overflow-y-auto') ?? document.querySelector('main');

  if (document.documentElement.scrollWidth > vw + 1) problems.push(`the page scrolls sideways at ${vw}px`);
  if (pane.scrollWidth > pane.clientWidth + 1)
    problems.push(`the pane scrolls sideways (${pane.scrollWidth} > ${pane.clientWidth})`);
  const paneBox = box(pane);
  const gutter = box(heading).left - paneBox.left;
  for (const el of pane.querySelectorAll('*')) {
    if (!shown(el) || el.closest('.sr-only')) continue;
    const r = box(el);
    if (!within(r, paneBox) || r.right > vw + 0.5)
      problems.push(`${name(el)} spans x=${Math.round(r.left)}–${Math.round(r.right)}, outside the pane`);
  }

  const cells = readouts ? [...(document.querySelector(readouts)?.children ?? [])] : [];
  for (const cell of cells) {
    const c = box(cell);
    const [row, ...below] = [...cell.children];
    const [label, meta] = [...(row?.children ?? [])].filter(shown);
    for (const part of [label, meta, ...below].filter((el) => el && shown(el))) {
      if (!within(box(part), c) || clipped(part))
        problems.push(`readout ${name(part)} does not fit its ${Math.round(c.width)}px cell`);
    }
    if (label && meta) {
      const [l, m] = [box(label), box(meta)];
      const sameLine = Math.min(l.bottom, m.bottom) > Math.max(l.top, m.top);
      if (sameLine && m.left - l.right < 4)
        problems.push(`readout ${name(label)} runs into ${name(meta)} (${(m.left - l.right).toFixed(1)}px apart)`);
    }
  }

  const rowEls = [...document.querySelectorAll(rows)].filter(shown);
  for (const row of rowEls) {
    const r = box(row);
    const kids = [...row.children].filter(shown);
    kids.forEach((kid, i) => {
      const k = box(kid);
      if (!within(k, r)) problems.push(`table cell ${name(kid)} leaves its row`);
      if (clipped(kid) && !kid.hasAttribute('data-run-title')) problems.push(`table cell ${name(kid)} is clipped`);
      for (const other of kids.slice(i + 1))
        if (overlap(k, box(other))) problems.push(`table cells ${name(kid)} and ${name(other)} overlap`);
    });
  }

  for (const head of [...document.querySelectorAll(heads)].filter(shown)) {
    const kids = [...head.children].filter((k) => shown(k) && k.textContent.trim());
    kids.forEach((kid, i) => {
      if (clipped(kid)) problems.push(`column head ${name(kid)} is clipped`);
      for (const other of kids.slice(i + 1))
        if (overlap(box(kid), box(other))) problems.push(`column heads ${name(kid)} and ${name(other)} overlap`);
    });
  }

  const filterEls = filters ? [...document.querySelectorAll(filters)] : [];
  const filterHeights = filterEls.map((el) => ({ text: el.textContent.trim(), height: box(el).height }));
  return {
    problems,
    gutter,
    filterHeights,
    counts: { readouts: cells.length, rows: rowEls.length, filters: filterEls.length }
  };
}

const PAGES = [
  {
    path: '/runs',
    ready: 'main button[aria-expanded]',
    probe: {
      h1: 'main h1',
      readouts: 'section[aria-label="Readouts"]',
      rows: 'main button[aria-expanded]',
      heads: '[data-runs-head]',
      filters: 'main button[aria-pressed]'
    },
    min: { readouts: 3, rows: 3, filters: 4 }
  },
  {
    path: '/budget',
    ready: 'section[aria-label="Budget caps"] [data-cap-row]',
    probe: {
      h1: 'main h1',
      readouts: 'section[aria-label="Spend against cap"]',
      rows: '[data-cap-row]',
      heads: '[data-cap-head]'
    },
    min: { readouts: 2, rows: 3 }
  },
  {
    // The fixture world serves Scorecard its data (#3461), so its readouts and
    // its model table are on the page to be read, not its offline sentence.
    path: '/scorecard',
    ready: '[data-scorecard-row]',
    probe: { h1: 'main h1', readouts: 'section[aria-label="Leverage"]', rows: '[data-scorecard-row]' },
    min: { readouts: 3, rows: 1 }
  }
];

export async function checks({ check, visit, expect, sleep, sizes: { WIDTH, HEIGHT } }) {
  for (const page of PAGES) {
    for (const size of [PHONE, TABLET, { width: WIDTH, height: HEIGHT }]) {
      await check(`${page.path} fits ${size.width}px: nothing clipped, run together or off the page`, () =>
        visit(
          page.path,
          async (p) => {
            await p.locator(page.ready).first().waitFor({ state: 'visible', timeout: 15_000 });
            await sleep(300);
            const read = await p.evaluate(readFit, page.probe);
            for (const [what, least] of Object.entries(page.min))
              expect(read.counts[what] >= least, `${page.path} drew ${read.counts[what]} ${what}, fewer than ${least}`);
            expect(read.problems.length === 0, `${page.path} at ${size.width}px:\n  ${read.problems.slice(0, 12).join('\n  ')}`);
            const want = size.width < 640 ? 16 : 40;
            expect(
              Math.abs(read.gutter - want) <= 1,
              `${page.path}'s gutter is ${read.gutter.toFixed(1)}px at ${size.width}px, not ${want}px`
            );
            if (size.width === PHONE.width) {
              for (const f of read.filterHeights)
                expect(f.height >= 32, `the "${f.text}" filter is ${f.height.toFixed(1)}px tall at 390px, under 32px`);
            }
          },
          size
        )
      );
    }
  }

  // Design review c9: Budget named a "calendar-aligned week" and "agentic (kew
  // dispatch) spend". The governor's window is today's UTC day and that day
  // with the six before it (`_spend_windows` in kew/budget.py), over every run
  // kew records, reviews included.
  await check('Budget states the window and the spend the caps are enforced against', () =>
    visit('/budget', async (page) => {
      const pane = page.locator('main').first();
      await pane.getByText('Weekly', { exact: true }).first().waitFor({ state: 'visible', timeout: 15_000 });
      const text = ((await pane.textContent()) ?? '').replace(/\s+/g, ' ');
      for (const said of [
        'every run kew records, reviews included',
        'today (UTC) for the daily cap, and today with the six days before it for the weekly cap',
        // The daily cap's row (the fixture's weekly row shows its override).
        'max spend across today (UTC)'
      ]) {
        expect(text.includes(said), `Budget does not say "${said}"`);
      }
      for (const wrong of ['calendar-aligned', 'agentic (kew dispatch)']) {
        expect(!text.includes(wrong), `Budget still says "${wrong}"`);
      }
    })
  );
}
