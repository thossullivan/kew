/**
 * Monitor: a long needs-you caption keeps every readout on screen (#2889).
 *
 * #2847 made the Needs you caption one line (`white-space: nowrap` and an
 * ellipsis). The readout row's tracks were bare `1fr`, and a `1fr` track never
 * shrinks below its content's min-content width, so a live hold reason of
 * about 300 characters widened the Needs you track and pushed Today and This
 * week past the right edge of a 1440 screen, which does not scroll sideways.
 * The fixture's caption is short, so nothing photographed it.
 *
 * Here the caption is a sentence of at least 300 characters, served through
 * `page.route`. At 1440 every readout's label and figure is inside the
 * viewport and the caption stays inside its own cell; at 390 all four are on
 * screen and nothing scrolls sideways. A sentence that long no longer ends in
 * an ellipsis: the cell says the queue by kind instead (#3052 N3), with the
 * sentence whole in its `title`.
 *
 * At 390 that summary breaks only between its phrases: "1 merge decision · 1
 * / hold" was a kind cut in two (#3468 D33).
 *
 * And the spend gauges, on `/monitor` and `/budget` (#3468 D15, #2953): no
 * gauge prints more than its two axis ends in money, the prior cap is a
 * caption under the axis, and with cap 0.001, prior cap 600 and spend 1000
 * served — the marker clamped to 100%, where the over-cap caption is drawn —
 * no two of a gauge's visible annotations intersect.
 *
 * One surface of `npm run ui-check`; `scripts/ui-check.mjs` runs it (#2570).
 */
// ui-check-owns: src/routes/(factory)/monitor/+page.*
// ui-check-owns: src/lib/monitor/readouts.ts
// ui-check-owns: src/lib/ui/Gauge.svelte
// ui-check-owns: src/lib/factory/gauge.ts
// ui-check-owns: src/routes/(factory)/budget/+page.svelte

const PHONE = { width: 390, height: 844 };

/** A hold reason the length of the one photographed live. */
const LONG_REASON =
  'the conflict resolve for #2766 could not finish its rebase after three attempts: the resolve ' +
  'executed, exit 1 (git rebase --continue stopped on src/kew/graph/driver/merge_queue.py with ' +
  'conflicting hunks in reconcile_publication and the frontier hint), and the held branch now ' +
  'waits on a human to pick the side that keeps the ledger marker and re-run the merge verify';

const withLongCaption = (page) =>
  page.route(
    (url) => url.pathname === '/api/dashboard',
    async (route) => {
      const response = await route.fetch();
      const body = await response.json();
      body.rows = body.rows.map((row) => ({ ...row, sentence: LONG_REASON }));
      await route.fulfill({ response, json: body });
    }
  );

/** #2953's numbers: a cap far under the spend, and a prior cap far over the cap. */
const CAP = 0.001;
const PRIOR_CAP = 600;
const SPEND = 1000;

/** Serve the weekly window at #2953's numbers, on every read a gauge is drawn from. */
async function withClampedPriorCap(page) {
  const weekly = (cell) => ({ ...cell, cap: CAP, spent: SPEND, agentic: SPEND, interactive: 0 });
  const patch = (path, edit) =>
    page.route(
      (url) => url.pathname === path,
      async (route) => {
        const response = await route.fetch();
        const body = await response.json();
        await route.fulfill({ response, json: edit(body) });
      }
    );
  await patch('/api/dashboard', (body) => {
    body.glance.caps.weekly = weekly(body.glance.caps.weekly);
    return body;
  });
  await patch('/api/report/spend', (body) => ({ ...body, weekly: weekly(body.weekly) }));
  await patch('/api/config/budget', (body) => ({
    ...body,
    'budget.weekly_usd': {
      effective: CAP,
      file_value: PRIOR_CAP,
      override: { value: CAP, set_by: 'operator', set_at: '2026-09-24T15:23:40+00:00', reason: '#2953' }
    }
  }));
}

/** Every drawn gauge's visible annotations: their text, box, and which is the caption. */
function readGauges() {
  return [...document.querySelectorAll('[data-gauge]')].map((gauge) => ({
    annotations: [...gauge.querySelectorAll('span')]
      .filter((el) => getComputedStyle(el).display !== 'none' && el.getClientRects().length > 0)
      .map((el) => {
        const r = el.getBoundingClientRect();
        return {
          text: el.textContent.trim(),
          caption: el.hasAttribute('data-gauge-marker-caption'),
          left: r.left,
          right: r.right,
          top: r.top,
          bottom: r.bottom
        };
      })
  }));
}

/**
 * Each phrase of the Needs you summary, and how many lines its glyphs sit on.
 * Phrases are what lies between the `·` separators.
 */
function readSummaryPhrases() {
  const summary = document.querySelector('[data-needs-you-caption] [data-caption-summary]');
  if (!summary) return null;
  const glyphs = [];
  const walker = document.createTreeWalker(summary, NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    for (let i = 0; i < node.data.length; i += 1) {
      const range = document.createRange();
      range.setStart(node, i);
      range.setEnd(node, i + 1);
      const rect = range.getBoundingClientRect();
      glyphs.push({ ch: node.data[i], top: Math.round(rect.top), width: rect.width });
    }
  }
  const phrases = [[]];
  for (const glyph of glyphs) {
    if (glyph.ch === '·') phrases.push([]);
    else phrases[phrases.length - 1].push(glyph);
  }
  return phrases
    .map((chars) => ({
      text: chars.map((c) => c.ch).join('').trim(),
      lines: new Set(chars.filter((c) => c.ch.trim() !== '' && c.width > 0).map((c) => c.top)).size
    }))
    .filter((phrase) => phrase.text !== '');
}

/** Each readout's label and figure boxes, and the Needs you caption as drawn. */
function readReadouts() {
  const section = document.querySelector('section[aria-label="Readouts"]');
  if (!section) return null;
  const box = (el) => {
    const r = el.getBoundingClientRect();
    return { left: r.left, right: r.right, top: r.top, bottom: r.bottom, width: r.width };
  };
  const cells = [...section.children].map((cell) => ({
    label: cell.children[0]?.children[0]?.textContent.trim() ?? '',
    labelBox: box(cell.children[0].children[0]),
    valueText: cell.children[1]?.textContent.replace(/\s+/g, ' ').trim() ?? '',
    valueBox: box(cell.children[1]),
    cellBox: box(cell)
  }));
  const caption = section.querySelector('[data-needs-you-caption]');
  // What the caption shows: the clamped sentence, or the summary by kind. The
  // probe that measures the sentence is never seen and is not read.
  const clip = caption?.querySelector('[data-caption-sentence] .truncate, [data-caption-summary]') ?? null;
  const scroller = section.parentElement;
  return {
    cells,
    caption: clip && {
      text: clip.textContent.replace(/\s+/g, ' ').trim(),
      title: caption.getAttribute('title') ?? '',
      box: box(clip),
      clipped: clip.scrollWidth > clip.clientWidth + 1
    },
    pageScrollsSideways: document.documentElement.scrollWidth > document.documentElement.clientWidth + 1,
    paneScrollsSideways: scroller.scrollWidth > scroller.clientWidth + 1
  };
}

const LABELS = [/working/i, /needs you/i, /today/i, /this week/i];

export async function checks({ check, visit, expect, sleep, sizes: { WIDTH, HEIGHT } }) {
  for (const size of [{ width: WIDTH, height: HEIGHT }, PHONE]) {
    await check(`a 300-character needs-you caption leaves all four readouts on screen at ${size.width}px`, () =>
      visit(
        '/monitor',
        async (page) => {
          await page.locator('[data-needs-you-caption]').waitFor({ state: 'visible', timeout: 15_000 });
          await sleep(300);
          const read = await page.evaluate(readReadouts);
          expect(read !== null, 'the Monitor drew no Readouts section');
          expect(read.cells.length === 4, `the Readouts section drew ${read.cells.length} cells, not 4`);
          expect(read.caption !== null, 'the Needs you cell drew no caption');
          expect(
            read.caption.title.length >= 300,
            `the caption's sentence is ${read.caption.title.length} characters, so the long reason never arrived`
          );
          LABELS.forEach((pattern, i) => {
            const cell = read.cells[i];
            expect(pattern.test(cell.label), `readout ${i + 1} is labelled "${cell.label}", not ${pattern}`);
            expect(cell.valueText !== '', `${cell.label} drew no figure`);
            for (const [part, b] of [
              ['label', cell.labelBox],
              ['figure', cell.valueBox]
            ]) {
              expect(b.width > 0, `${cell.label}'s ${part} has no width`);
              expect(
                b.left >= 0 && b.right <= size.width + 0.5,
                `${cell.label}'s ${part} spans x=${Math.round(b.left)}–${Math.round(b.right)} in a ${size.width}px viewport`
              );
              expect(
                b.top >= 0 && b.bottom <= size.height,
                `${cell.label}'s ${part} spans y=${Math.round(b.top)}–${Math.round(b.bottom)} in a ${size.height}px viewport`
              );
            }
          });
          expect(!read.pageScrollsSideways, `the page scrolls sideways at ${size.width}px`);
          expect(!read.paneScrollsSideways, `the Monitor pane scrolls sideways at ${size.width}px`);
          if (size.width === WIDTH) {
            const needsYou = read.cells[1].cellBox;
            // #3052 N3: a sentence that long is not cut to an ellipsis; the
            // cell says the queue by kind.
            expect(!read.caption.clipped, `the caption "${read.caption.text}" is cut at ${size.width}px`);
            expect(read.caption.text.length < 300, `the cell draws the whole ${read.caption.text.length}-character sentence`);
            expect(
              read.caption.box.left >= needsYou.left && read.caption.box.right <= needsYou.right + 0.5,
              `the caption spans x=${Math.round(read.caption.box.left)}–${Math.round(read.caption.box.right)}, ` +
                `outside the Needs you cell at x=${Math.round(needsYou.left)}–${Math.round(needsYou.right)}`
            );
          }
          if (size.width === PHONE.width) {
            const phrases = await page.evaluate(readSummaryPhrases);
            expect(phrases !== null, `the Needs you cell drew no summary by kind at ${size.width}px`);
            expect(phrases.length >= 2, `the summary has ${phrases.length} phrase(s), so no break between them was tried`);
            for (const phrase of phrases) {
              expect(phrase.lines === 1, `the summary breaks inside "${phrase.text}" (${phrase.lines} lines)`);
            }
          }
        },
        size,
        withLongCaption
      )
    );
  }

  const intersect = (a, b) => a.left < b.right - 0.5 && b.left < a.right - 0.5 && a.top < b.bottom - 0.5 && b.top < a.bottom - 0.5;
  for (const route of ['/monitor', '/budget']) {
    for (const size of [{ width: WIDTH, height: HEIGHT }, PHONE]) {
      await check(`no ${route} gauge prints a third money label or overlaps its annotations at ${size.width}px`, () =>
        visit(
          route,
          async (page) => {
            await page.locator('[data-gauge-marker-caption]').first().waitFor({ state: 'visible', timeout: 15_000 });
            await sleep(300);
            const gauges = await page.evaluate(readGauges);
            expect(gauges.length >= 2, `${route} drew ${gauges.length} gauges, not both windows`);
            const marked = gauges.filter((g) => g.annotations.some((a) => a.caption));
            expect(marked.length >= 1, `no ${route} gauge names the served prior cap`);
            const over = gauges.filter((g) => g.annotations.some((a) => /over cap/.test(a.text)));
            expect(
              over.some((g) => marked.includes(g)),
              `no ${route} gauge is both over cap and marked, so #2953's clash was never drawn`
            );
            for (const gauge of gauges) {
              const axis = gauge.annotations.filter((a) => !a.caption && a.text.includes('$'));
              expect(
                axis.length <= 2,
                `a ${route} gauge prints ${axis.length} axis money labels: ${axis.map((a) => a.text).join(' | ')}`
              );
              for (const caption of gauge.annotations.filter((a) => a.caption)) {
                for (const label of axis) {
                  expect(
                    caption.top >= label.bottom - 0.5,
                    `"${caption.text}" is not under the axis label "${label.text}"`
                  );
                }
              }
              const all = gauge.annotations;
              for (let i = 0; i < all.length; i += 1) {
                for (let j = i + 1; j < all.length; j += 1) {
                  expect(!intersect(all[i], all[j]), `a ${route} gauge paints "${all[i].text}" over "${all[j].text}"`);
                }
              }
            }
            const sideways = await page.evaluate(
              () => document.documentElement.scrollWidth > document.documentElement.clientWidth + 1
            );
            expect(!sideways, `${route} scrolls sideways at ${size.width}px`);
          },
          size,
          withClampedPriorCap
        )
      );
    }
  }
}
