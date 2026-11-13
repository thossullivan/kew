/**
 * The Prompts page's outcomes per version (#3026): at 1440 and 390 the
 * implementer table renders one row per fixture version, newest first,
 * built-in last, with no row the fixture lacks; its numbers are mono and
 * right-aligned; a version with fewer than ten items shows "Too few items to
 * compare yet (n = 4)." instead of rates. At 1440 the reviewer's table does
 * the same and counts rounds in one right-aligned mono cell per verdict
 * column, so its counts line up. Where the outcomes section is narrower
 * than its `@3xl` container size (768px) the reviewer draws one block per
 * version instead (the table is hidden): its rounds, then each verdict word
 * with its mono count, or the too-few sentence; at 390 the scenario's blocks
 * read so. The section's width, not the window's, picks the layout: from
 * `md` up the page is a 400px aside beside the pane, so a 768 window leaves
 * it 288px. Served six verdicts at 390, 640, 768, 800, 1024 and 1280, and
 * seven at 768, 1024 and 1280, the layout each section width calls for
 * shows; the section stays inside the viewport; every box stays inside the
 * section; neither the section nor any ancestor overflows sideways, whether
 * it scrolls or clips (the page's own wrapper scrolls, so a document width
 * check alone cannot fail there); every verdict word is on one line; and
 * every served verdict shows with its count. Negative controls expect that
 * check to fail on a 600px verdict list, on words let wrap, on the
 * six-column table drawn at 390 and at 1024 (where the blocks show), and on
 * a 600px section cut off by `main`'s overflow-hidden; and the block check
 * to fail on a wrong count and on a missing pair. Nothing scrolls
 * sideways, and a failed read prints a sentence. Approving v1 in the editor
 * reads the table again, and v1 then has its own row, active, at n = 0.
 *
 * The table is drawn only from the generation of the history the editor
 * shows: an outcomes read one decision ahead of the versions read (a CLI
 * approval between the two) is refused and read again with the versions, and
 * the table never sits beside the history it does not match; one that never
 * matches prints a sentence after the bounded re-reads.
 *
 * The answers are `fixtures/scenarios/prompts-outcomes.json`, generated from
 * the real API over one seeded store (outcomes, history and preview of each
 * role, so their generations agree); each check routes its own.
 *
 * One surface of `npm run ui-check`; `scripts/ui-check.mjs` runs it.
 */
// ui-check-owns: src/lib/prompts/ProfileOutcomes.svelte
// ui-check-owns: src/lib/prompts/outcomes.ts
// ui-check-owns: src/lib/prompts/ProfileEditor.svelte
// ui-check-owns: src/routes/(factory)/prompts/+page.svelte
// ui-check-owns: fixtures/scenarios/prompts-outcomes.json
// ui-check-owns: fixtures/factory/prompts-*-outcomes.json
import { readFileSync } from 'node:fs';
import { FRONTEND, widestBox } from '../ui-check-lib.mjs';

const PHONE = { width: 390, height: 844 };
const SCENARIO = JSON.parse(readFileSync(new URL('fixtures/scenarios/prompts-outcomes.json', FRONTEND), 'utf8'));

/** The editor's history and preview answers for one proposal and its approval (#3025). */
const VERSIONS = JSON.parse(readFileSync(new URL('fixtures/scenarios/prompts-versions.json', FRONTEND), 'utf8'));

const isPath = (path) => (url) => url.pathname === path;
const text = async (locator) => (await locator.innerText()).replace(/\s+/g, ' ').trim();

/**
 * Serve `role`'s outcomes read as `answer` (`json`, or a bare `status`), beside
 * the history and preview the same store served: the table is drawn only from
 * the generation of the history the editor shows.
 */
async function serveOutcomes(page, role, answer) {
  await page.route(isPath(`/api/prompts/${role}/versions`), (route) =>
    route.fulfill({ json: SCENARIO.history[role] })
  );
  await page.route(isPath(`/api/prompts/${role}`), (route) => route.fulfill({ json: SCENARIO.preview[role] }));
  await page.route(isPath(`/api/prompts/${role}/outcomes`), (route) => route.fulfill(answer));
}

/** Every number cell's font family and alignment, in the outcomes table. */
function numberCells() {
  return [...document.querySelectorAll('[data-outcome-n], [data-outcome-cell]')]
    .filter((el) => /\d/.test(el.textContent ?? ''))
    .map((el) => {
      const style = getComputedStyle(el);
      return {
        text: el.textContent.trim(),
        verdict: el.getAttribute('data-verdict'),
        font: style.fontFamily,
        align: style.textAlign
      };
    });
}

const percent = (rate) => (rate === null ? '—' : `${Math.round(rate * 100)}%`);
const mean = (value) => (value === null ? '—' : value.toFixed(1));
const dollars = (value) => (value === null ? '—' : `$${value.toFixed(2)}`);

/** The verdicts comparable versions returned, most returned across them first, then by name. */
function verdictColumns(versions) {
  const totals = new Map();
  for (const v of versions.filter((row) => row.comparable)) {
    for (const [verdict, n] of Object.entries(v.verdicts)) totals.set(verdict, (totals.get(verdict) ?? 0) + n);
  }
  return [...totals.entries()].sort(([a, x], [b, y]) => y - x || a.localeCompare(b)).map(([verdict]) => verdict);
}

/** Throw unless the page and its boxes stay inside the viewport, and nothing errored. */
async function fits(page, problems, expect) {
  const box = await widestBox(page);
  expect(box.w <= box.c + 1, `scrolls sideways: ${box.w} in ${box.c}, ${box.culprit}`);
  expect(problems.length === 0, problems.join('\n'));
}

/** The verdicts added to the scenario's three: six in all, a table 686px wide. */
const SIX = { READY_WITH_RESIDUALS: 9, REJECTED: 4, DEGRADED: 3, BLOCKED: 2 };
/** Seven, with ERROR: a table 739px wide, the widest the section's `@3xl`
 *  (768px) switch is chosen for. */
const SEVEN = { ...SIX, ERROR: 1 };

/** The scenario's reviewer answer with its comparable version's rounds spread
 *  over `extra` verdicts as well. */
function servedVerdicts(extra) {
  const read = structuredClone(SCENARIO.adversarial_review);
  for (const v of read.versions.filter((row) => row.comparable)) {
    Object.assign(v.verdicts, extra);
    v.n = Object.values(v.verdicts).reduce((sum, n) => sum + n, 0);
  }
  return read;
}

/** Which reviewer layout is on screen: 'table', 'blocks', or both / neither. */
async function shownLayout(page) {
  const table = await page.locator('[data-outcomes-table="reviewer"]').isVisible();
  const blocks = await page.locator('[data-outcomes-blocks="reviewer"]').isVisible();
  if (table && blocks) return 'both';
  if (table) return 'table';
  return blocks ? 'blocks' : 'neither';
}

/** Wait for the reviewer outcomes, in either layout. */
const reviewerDrawn = (page) =>
  page
    .locator('[data-outcomes-table="reviewer"], [data-outcomes-blocks="reviewer"]')
    .first()
    .waitFor({ state: 'attached', timeout: 15_000 });

/**
 * The windows the fit check reads, with the section width each leaves and
 * the layout that must show there. From `md` (768) up the page is a 400px
 * aside beside the pane, so the section is the window minus 480px: 768 leaves
 * 288px, narrower than a phone's 350. The table shows from a 768px section.
 */
const FIT_WINDOWS = [
  { width: 390, height: 844, section: 350, layout: 'blocks' },
  { width: 640, height: 900, section: 600, layout: 'blocks' },
  { width: 768, height: 900, section: 288, layout: 'blocks' },
  { width: 800, height: 900, section: 320, layout: 'blocks' },
  { width: 1024, height: 900, section: 544, layout: 'blocks' },
  { width: 1280, height: 900, section: 800, layout: 'table' }
];

/** Each phone block as drawn: its version, rounds, verdict pairs as
 *  [verdict, word, count], and its sentence. */
const readBlocks = (page) =>
  page.locator('[data-outcome-block]').evaluateAll((blocks) =>
    blocks.map((b) => ({
      label: b.getAttribute('data-outcome-block'),
      n: b.querySelector('[data-block-n]')?.textContent.trim() ?? null,
      pairs: [...b.querySelectorAll('[data-verdict-pair]')].map((pair) => [
        pair.getAttribute('data-verdict-pair'),
        pair.querySelector('[data-status-word]')?.textContent.trim() ?? null,
        pair.querySelector('[data-pair-count]')?.textContent.trim() ?? null
      ]),
      few: b.querySelector('[data-outcome-few]')?.textContent.replace(/\s+/g, ' ').trim() ?? null
    }))
  );

/** How the phone blocks differ from `read`: one block per version in its
 *  order, its rounds, and every served verdict with its count, in column
 *  order — or the sentence for a version with too few rounds. */
function blockMismatches(read, blocks) {
  const wrong = [];
  const labels = blocks.map((b) => b.label);
  const wanted = read.versions.map((v) => v.label);
  if (JSON.stringify(labels) !== JSON.stringify(wanted)) {
    wrong.push(`the blocks read [${labels.join(', ')}], not [${wanted.join(', ')}]`);
  }
  const columns = verdictColumns(read.versions);
  for (const v of read.versions) {
    const block = blocks.find((b) => b.label === v.label);
    if (!block) continue;
    if (block.n !== String(v.n)) wrong.push(`${v.label} shows ${block.n} rounds, not ${v.n}`);
    if (v.comparable) {
      const want = columns.map((verdict) => [verdict, verdict.replaceAll('_', ' '), String(v.verdicts[verdict] ?? 0)]);
      if (JSON.stringify(block.pairs) !== JSON.stringify(want)) {
        wrong.push(`${v.label} pairs ${JSON.stringify(block.pairs)}, not ${JSON.stringify(want)}`);
      }
      if (block.few !== null) wrong.push(`${v.label} has ${v.n} rounds and still says too few`);
    } else {
      const want = `Too few rounds to compare yet (n = ${v.n}).`;
      if (block.few !== want) wrong.push(`${v.label} reads "${block.few}", not "${want}"`);
      if (block.pairs.length > 0) wrong.push(`${v.label} shows counts beside the sentence: ${JSON.stringify(block.pairs)}`);
    }
  }
  return wrong;
}

/** Everything wrong with the reviewer outcomes in `page`, in either layout,
 *  as sentences: the section past the viewport's right edge, a box inside
 *  it past its edges, the section or any ancestor up to the document
 *  overflowing sideways — one that scrolls (auto, scroll) or one that clips
 *  (hidden, clip); the page's own wrapper scrolls, so the document check
 *  alone cannot fail — a verdict word over one line, or a block that does
 *  not read `read`. Returned, not thrown, so the negative controls can
 *  expect a failure. */
async function outcomesFit(page, read) {
  await page.evaluate(() => document.fonts.ready);
  const layout = await page.evaluate(() => {
    const wrong = [];
    const section = document.querySelector('section[data-profile-outcomes]');
    if (!section) return ['no outcomes section was found'];
    const name = (el) =>
      `<${el.tagName.toLowerCase()}${[...el.attributes]
        .filter((a) => a.name.startsWith('data-'))
        .map((a) => ` ${a.name}${a.value ? `="${a.value}"` : ''}`)
        .join('')}>`;
    const edge = section.getBoundingClientRect();
    if (edge.right > window.innerWidth + 1) {
      wrong.push(`the section ends at ${Math.round(edge.right)}px, past the viewport's ${window.innerWidth}px`);
    }
    if (section.scrollWidth > section.clientWidth + 1) {
      wrong.push(`the section scrolls sideways: ${section.scrollWidth}px in ${section.clientWidth}px`);
    }
    for (const el of section.querySelectorAll('*')) {
      if (el.getClientRects().length === 0) continue;
      const box = el.getBoundingClientRect();
      if (box.right > edge.right + 1 || box.left < edge.left - 1) {
        wrong.push(
          `${name(el)} runs ${Math.round(box.left)}-${Math.round(box.right)}px, past the section's ${Math.round(edge.left)}-${Math.round(edge.right)}px`
        );
      }
    }
    // An ancestor that scrolls (auto, scroll) lets the pane be dragged sideways;
    // one that clips (hidden, clip) cuts the outcomes off. Either fails.
    for (let p = section.parentElement; p; p = p.parentElement) {
      const o = getComputedStyle(p).overflowX;
      const bounded = p === document.documentElement || o !== 'visible';
      if (bounded && p.scrollWidth > p.clientWidth + 1) {
        wrong.push(`${name(p)} (overflow-x ${o}) overflows sideways: ${p.scrollWidth}px in ${p.clientWidth}px`);
      }
    }
    const words = [...section.querySelectorAll('[data-status-word]')].filter((w) => w.getClientRects().length > 0);
    if (words.length === 0) wrong.push('no verdict word is drawn in the section');
    for (const word of words) {
      const style = getComputedStyle(word);
      const line = parseFloat(style.lineHeight) || parseFloat(style.fontSize) * 1.5;
      // The text's own height: a table head carries the word with its padding.
      const height =
        word.getBoundingClientRect().height -
        ['paddingTop', 'paddingBottom', 'borderTopWidth', 'borderBottomWidth'].reduce(
          (sum, side) => sum + (parseFloat(style[side]) || 0),
          0
        );
      if (height > line * 1.5) {
        wrong.push(`"${word.textContent.trim()}" is ${Math.round(height)}px tall, over one ${Math.round(line)}px line`);
      }
    }
    return wrong;
  });
  return [...layout, ...blockMismatches(read, await readBlocks(page))];
}

export async function checks({ check, visit, expect, sizes: { WIDTH, HEIGHT } }) {
  for (const size of [{ width: WIDTH, height: HEIGHT }, PHONE]) {
    await check(`/prompts tables the dispatch outcomes per version at ${size.width}px`, () => {
      const read = SCENARIO.dispatch;
      return visit(
        '/prompts?role=dispatch',
        async (page, problems) => {
          const table = page.locator('[data-outcomes-table="implementer"]');
          await table.waitFor({ state: 'visible', timeout: 15_000 });
          await table.scrollIntoViewIfNeeded();

          const labels = await page.locator('[data-outcome-row]').evaluateAll((rows) =>
            rows.map((r) => r.getAttribute('data-outcome-row'))
          );
          const wanted = read.versions.map((v) => v.label);
          expect(
            JSON.stringify(labels) === JSON.stringify(wanted),
            `the rows read [${labels.join(', ')}], not [${wanted.join(', ')}]`
          );
          expect(wanted.at(-1) === 'built-in', 'the fixture has no built-in row last');

          const few = read.versions.filter((v) => !v.comparable);
          expect(few.length > 0 && read.versions.some((v) => v.comparable), 'the fixture lacks a row of each kind');
          for (const v of read.versions) {
            const row = page.locator(`[data-outcome-row="${v.label}"]`);
            const n = await text(row.locator('[data-outcome-n]'));
            expect(n === String(v.n), `${v.label} shows ${n} items, not ${v.n}`);
            const cells = await row.locator('[data-outcome-cell]').allInnerTexts();
            const sentence = row.locator('[data-outcome-few]');
            if (v.comparable) {
              const rates = [percent(v.first_pass_rate), mean(v.mean_rounds_to_ready), dollars(v.median_cost_usd)];
              expect(
                JSON.stringify(cells) === JSON.stringify(rates),
                `${v.label} shows [${cells.join(', ')}], not [${rates.join(', ')}]`
              );
              expect((await sentence.count()) === 0, `${v.label} has ${v.n} items and still says too few`);
            } else {
              const said = await text(sentence);
              const want = `Too few items to compare yet (n = ${v.n}).`;
              expect(said === want, `${v.label} reads "${said}", not "${want}"`);
              expect(cells.length === 0, `${v.label} shows rates beside the sentence: ${cells.join(', ')}`);
              expect(
                !(await text(row)).includes('%'),
                `${v.label} has ${v.n} items and still shows a percentage`
              );
            }
          }

          const numbers = await page.evaluate(numberCells);
          expect(numbers.length > 0, 'no number cell was found');
          const off = numbers.filter((c) => !/Plex Mono/i.test(c.font) || c.align !== 'right');
          expect(
            off.length === 0,
            `numbers not mono and right-aligned: ${off.map((c) => `${c.text} (${c.font}, ${c.align})`).join('; ')}`
          );
          await fits(page, problems, expect);
        },
        size,
        (page) => serveOutcomes(page, 'dispatch', { json: read })
      );
    });

    // Below an @3xl section the reviewer draws one block per version: the 390 checks follow this loop.
    if (size === PHONE) continue;

    await check(`/prompts tables the reviewer's verdicts per version at ${size.width}px`, () => {
      const read = SCENARIO.adversarial_review;
      return visit(
        '/prompts?role=adversarial_review',
        async (page, problems) => {
          const table = page.locator('[data-outcomes-table="reviewer"]');
          await table.waitFor({ state: 'visible', timeout: 15_000 });
          await table.scrollIntoViewIfNeeded();
          expect(
            !(await page.locator('[data-outcomes-blocks="reviewer"]').isVisible()),
            `the phone blocks are drawn beside the table at ${size.width}px`
          );

          const labels = await page.locator('[data-outcome-row]').evaluateAll((rows) =>
            rows.map((r) => r.getAttribute('data-outcome-row'))
          );
          const wanted = read.versions.map((v) => v.label);
          expect(
            JSON.stringify(labels) === JSON.stringify(wanted),
            `the rows read [${labels.join(', ')}], not [${wanted.join(', ')}]`
          );
          expect(wanted.at(-1) === 'built-in', 'the fixture has no built-in row last');

          // One column per verdict a comparable version returned, most returned first.
          const columns = verdictColumns(read.versions);
          expect(columns.length > 0, 'the fixture has no comparable reviewer version');
          const heads = await page
            .locator('[data-verdict-head]')
            .evaluateAll((cells) => cells.map((c) => c.getAttribute('data-verdict-head')));
          expect(
            JSON.stringify(heads) === JSON.stringify(columns),
            `the verdict columns read [${heads.join(', ')}], not [${columns.join(', ')}]`
          );

          for (const v of read.versions) {
            const row = page.locator(`[data-outcome-row="${v.label}"]`);
            const n = await text(row.locator('[data-outcome-n]'));
            expect(n === String(v.n), `${v.label} shows ${n} rounds, not ${v.n}`);
            const cells = await row
              .locator('[data-outcome-cell]')
              .evaluateAll((tds) => tds.map((td) => [td.getAttribute('data-verdict'), td.textContent.trim()]));
            const sentence = row.locator('[data-outcome-few]');
            if (v.comparable) {
              const want = columns.map((verdict) => [verdict, String(v.verdicts[verdict] ?? 0)]);
              expect(
                JSON.stringify(cells) === JSON.stringify(want),
                `${v.label} shows ${JSON.stringify(cells)}, not ${JSON.stringify(want)}`
              );
              expect((await sentence.count()) === 0, `${v.label} has ${v.n} rounds and still says too few`);
            } else {
              const said = await text(sentence);
              const want = `Too few rounds to compare yet (n = ${v.n}).`;
              expect(said === want, `${v.label} reads "${said}", not "${want}"`);
              expect(cells.length === 0, `${v.label} shows counts beside the sentence: ${JSON.stringify(cells)}`);
            }
          }

          const numbers = await page.evaluate(numberCells);
          const counts = numbers.filter((c) => c.verdict !== null);
          expect(counts.length > 0, 'no verdict count cell was found');
          const off = numbers.filter((c) => !/Plex Mono/i.test(c.font) || c.align !== 'right');
          expect(
            off.length === 0,
            `numbers not mono and right-aligned: ${off.map((c) => `${c.text} (${c.font}, ${c.align})`).join('; ')}`
          );
          await fits(page, problems, expect);
        },
        size,
        (page) => serveOutcomes(page, 'adversarial_review', { json: read })
      );
    });
  }

  /**
   * A version label is an id, and an id never wraps: at 390 the built-in row's
   * label is one line, not "built-" over "in", and so is the column's header.
   */
  await check('/prompts keeps every version label on one line at 390px', () =>
    visit(
      '/prompts?role=dispatch',
      async (page) => {
        const table = page.locator('[data-outcomes-table="implementer"]');
        await table.waitFor({ state: 'visible', timeout: 15_000 });
        const lines = await table.evaluate((el) => {
          const lineCount = (node) => {
            const range = document.createRange();
            range.selectNodeContents(node);
            return new Set([...range.getClientRects()].map((r) => Math.round(r.top))).size;
          };
          const head = [...el.querySelectorAll('thead th')].find((th) => th.textContent.trim() === 'Version');
          return [
            ...[...el.querySelectorAll('[data-outcome-label]')].map((label) => [label.textContent.trim(), lineCount(label)]),
            ['the Version header', head ? lineCount(head) : 0]
          ];
        });
        expect(lines.some(([label]) => label === 'built-in'), 'the table has no built-in label to check');
        const broken = lines.filter(([, count]) => count !== 1);
        expect(broken.length === 0, `labels not one line tall: ${broken.map(([l, c]) => `${l} (${c} lines)`).join(', ')}`);
      },
      PHONE,
      (page) => serveOutcomes(page, 'dispatch', { json: SCENARIO.dispatch })
    )
  );

  await check("/prompts draws the reviewer's verdicts as one block per version at 390px", () => {
    const read = SCENARIO.adversarial_review;
    return visit(
      '/prompts?role=adversarial_review',
      async (page, problems) => {
        const blocks = page.locator('[data-outcomes-blocks="reviewer"]');
        await blocks.waitFor({ state: 'visible', timeout: 15_000 });
        await blocks.scrollIntoViewIfNeeded();
        expect(
          !(await page.locator('[data-outcomes-table="reviewer"]').isVisible()),
          'the reviewer table is drawn beside the phone blocks at 390px'
        );
        expect(
          read.versions.some((v) => v.comparable) && read.versions.some((v) => !v.comparable),
          'the fixture lacks a block of each kind'
        );
        const wrong = blockMismatches(read, await readBlocks(page));
        expect(wrong.length === 0, wrong.join('\n'));

        const numbers = await page
          .locator('[data-block-n], [data-pair-count]')
          .evaluateAll((els) => els.map((el) => ({ text: el.textContent.trim(), font: getComputedStyle(el).fontFamily })));
        expect(numbers.length > read.versions.length, 'no verdict count was found in the blocks');
        const off = numbers.filter((c) => !/Plex Mono/i.test(c.font));
        expect(off.length === 0, `numbers not mono: ${off.map((c) => `${c.text} (${c.font})`).join('; ')}`);
        await fits(page, problems, expect);
      },
      PHONE,
      (page) => serveOutcomes(page, 'adversarial_review', { json: read })
    );
  });

  const serveSix = (page) => serveOutcomes(page, 'adversarial_review', { json: servedVerdicts(SIX) });

  /** Six served verdicts at every window, and seven where the section is
   *  narrowest beside the aside and where the table first shows: the
   *  section's width picks the layout, never the window's, and whichever
   *  shows fits. */
  const fitRuns = [
    ...FIT_WINDOWS.map((w) => ({ ...w, extra: SIX, count: 6 })),
    ...FIT_WINDOWS.filter((w) => [768, 1024, 1280].includes(w.width)).map((w) => ({ ...w, extra: SEVEN, count: 7 }))
  ];
  for (const run of fitRuns) {
    await check(
      `/prompts keeps ${run.count} served verdicts inside its ${run.section}px outcomes section at ${run.width}px, as ${run.layout}`,
      () => {
        const read = servedVerdicts(run.extra);
        return visit(
          '/prompts?role=adversarial_review',
          async (page, problems) => {
            await reviewerDrawn(page);
            const count = verdictColumns(read.versions).length;
            expect(count === run.count, `the answer serves ${count} comparable verdicts, not ${run.count}`);
            const section = await page.locator('section[data-profile-outcomes]').evaluate((s) => s.clientWidth);
            expect(
              Math.abs(section - run.section) <= 1,
              `the section is ${section}px at ${run.width}px, not ${run.section}px`
            );
            const layout = await shownLayout(page);
            expect(layout === run.layout, `the ${section}px section shows ${layout}, not ${run.layout}`);
            const failures = await outcomesFit(page, read);
            expect(failures.length === 0, failures.join('\n'));
            await fits(page, problems, expect);
          },
          { width: run.width, height: run.height },
          (page) => serveOutcomes(page, 'adversarial_review', { json: read })
        );
      }
    );
  }

  /** The negative control: the same page with its verdict list forced wider
   *  than the section is read as running out of it and sideways; with its
   *  words let wrap at their narrowest, as wrapped — so a pass above means
   *  the block fits and every word is on one line. */
  await check('the 390px outcomes fit check catches a phone block wider than its section', () =>
    visit(
      '/prompts?role=adversarial_review',
      async (page) => {
        const read = servedVerdicts(SIX);
        await page.locator('[data-outcomes-blocks="reviewer"]').waitFor({ state: 'visible', timeout: 15_000 });
        const wide = await page.addStyleTag({ content: '[data-verdict-pairs] { min-width: 600px !important; }' });
        const overflow = await outcomesFit(page, read);
        expect(
          overflow.some((f) => f.includes('past the section')) && overflow.some((f) => f.includes('sideways')),
          `the check did not flag a 600px verdict list; it flagged: ${overflow.join('; ') || 'nothing'}`
        );
        await wide.evaluate((tag) => tag.remove());

        await page.addStyleTag({
          content:
            '[data-verdict-pair], [data-verdict-pair] [data-status-word] { white-space: normal !important; } [data-verdict-pair] [data-status-word] { width: min-content !important; }'
        });
        const wrapped = await outcomesFit(page, read);
        expect(
          wrapped.some((f) => f.includes('over one')),
          `the check did not flag a wrapped verdict word; it flagged: ${wrapped.join('; ') || 'nothing'}`
        );
      },
      PHONE,
      serveSix
    )
  );

  /** The layout this replaced, drawn where the blocks show: the six-column
   *  table (686px) in a phone's 350px section and in the 544px one a 1024
   *  window leaves beside the aside — the window `sm` let it show in. */
  for (const size of [PHONE, { width: 1024, height: 900 }]) {
    await check(`the ${size.width}px outcomes fit check catches the six-column table`, () =>
      visit(
        '/prompts?role=adversarial_review',
        async (page) => {
          const read = servedVerdicts(SIX);
          await page.locator('[data-outcomes-blocks="reviewer"]').waitFor({ state: 'visible', timeout: 15_000 });
          expect((await shownLayout(page)) === 'blocks', `the blocks do not show at ${size.width}px`);
          await page.addStyleTag({
            content:
              '[data-outcomes-table="reviewer"] { display: table !important; } [data-outcomes-blocks] { display: none !important; }'
          });
          const tabled = await outcomesFit(page, read);
          expect(
            tabled.some((f) => f.includes('past the section')) && tabled.some((f) => f.includes('sideways')),
            `the check did not flag the six-column table at ${size.width}px; it flagged: ${tabled.join('; ') || 'nothing'}`
          );
        },
        size,
        serveSix
      )
    );
  }

  /** A section wider than the window: read as past the viewport, and — with
   *  the page's scrolling wrapper let overflow — as overflowing `main`, which
   *  clips (overflow-x hidden) and so never scrolls to show what it cut. */
  await check('the 390px outcomes fit check catches a section cut off by a clipping ancestor', () =>
    visit(
      '/prompts?role=adversarial_review',
      async (page) => {
        const read = servedVerdicts(SIX);
        await page.locator('[data-outcomes-blocks="reviewer"]').waitFor({ state: 'visible', timeout: 15_000 });
        await page.addStyleTag({ content: 'section[data-profile-outcomes] { min-width: 600px !important; }' });
        const wide = await outcomesFit(page, read);
        expect(
          wide.some((f) => f.includes('past the viewport')),
          `the check did not flag a 600px section in a 390px window; it flagged: ${wide.join('; ') || 'nothing'}`
        );

        const wrapper = await page.locator('section[data-profile-outcomes]').evaluate((section) => {
          for (let p = section.parentElement; p; p = p.parentElement) {
            if (getComputedStyle(p).overflowX === 'auto') {
              p.style.overflow = 'visible';
              return true;
            }
          }
          return false;
        });
        expect(wrapper, 'no scrolling wrapper was found above the section');
        const clipped = await outcomesFit(page, read);
        expect(
          clipped.some((f) => f.startsWith('<main') && f.includes('(overflow-x hidden)')),
          `the check did not flag main clipping the section; it flagged: ${clipped.join('; ') || 'nothing'}`
        );
      },
      PHONE,
      serveSix
    )
  );

  /** The block reader's control: a pair showing a wrong count, then a block
   *  missing a pair, are each read as not what was served. */
  await check('the 390px outcomes block check catches a wrong count and a missing pair', () =>
    visit(
      '/prompts?role=adversarial_review',
      async (page) => {
        const read = servedVerdicts(SIX);
        const comparable = read.versions.find((v) => v.comparable).label;
        const block = page.locator(`[data-outcome-block="${comparable}"]`);
        await block.waitFor({ state: 'visible', timeout: 15_000 });
        expect(blockMismatches(read, await readBlocks(page)).length === 0, 'the blocks are wrong before the control');

        const count = block.locator('[data-pair-count]').first();
        const was = await count.evaluate((el) => {
          const before = el.textContent;
          el.textContent = '999';
          return before;
        });
        const miscounted = blockMismatches(read, await readBlocks(page));
        expect(
          miscounted.some((f) => f.startsWith(`${comparable} pairs`)),
          `the check did not flag a count of 999; it flagged: ${miscounted.join('; ') || 'nothing'}`
        );
        await count.evaluate((el, text) => (el.textContent = text), was);

        await block.locator('[data-verdict-pair]').last().evaluate((el) => el.remove());
        const missing = blockMismatches(read, await readBlocks(page));
        expect(
          missing.some((f) => f.startsWith(`${comparable} pairs`)),
          `the check did not flag a missing pair; it flagged: ${missing.join('; ') || 'nothing'}`
        );
      },
      PHONE,
      serveSix
    )
  );

  for (const size of [{ width: WIDTH, height: HEIGHT }, PHONE]) {
    await check(`/prompts reads the outcomes again after an approval at ${size.width}px`, () => {
      const { before, after } = SCENARIO.approval;
      const state = { history: VERSIONS.proposed, approved: false, reads: [] };
      const prepare = async (page) => {
        await page.route(isPath('/api/prompts/dispatch/versions'), (route) =>
          route.fulfill({ json: state.history })
        );
        await page.route(isPath('/api/prompts/dispatch/versions/1/approve'), (route) => {
          state.history = VERSIONS.approved;
          state.approved = true;
          return route.fulfill({ json: state.history });
        });
        await page.route(isPath('/api/prompts/dispatch'), (route) =>
          state.approved ? route.fulfill({ json: VERSIONS.preview_approved }) : route.fallback()
        );
        await page.route(isPath('/api/prompts/dispatch/outcomes'), (route) => {
          state.reads.push(state.approved ? 'after' : 'before');
          return route.fulfill({ json: state.approved ? after : before });
        });
      };
      return visit(
        '/prompts?role=dispatch',
        async (page, problems) => {
          const rows = () =>
            page.locator('[data-outcome-row]').evaluateAll((els) => els.map((r) => r.getAttribute('data-outcome-row')));
          await page.locator('[data-outcomes-table="implementer"]').waitFor({ state: 'visible', timeout: 15_000 });
          const first = await rows();
          const wantFirst = before.versions.map((v) => v.label);
          expect(
            JSON.stringify(first) === JSON.stringify(wantFirst),
            `before the approval the rows read [${first.join(', ')}], not [${wantFirst.join(', ')}]`
          );

          await page.locator('[data-pending="1"]').waitFor({ state: 'visible', timeout: 15_000 });
          await page.locator('[data-approve]').click();
          await page.locator('[data-outcome-row="v1"]').waitFor({ state: 'visible', timeout: 15_000 });

          expect(
            state.reads.includes('after'),
            `no outcomes read followed the approval: [${state.reads.join(', ')}]`
          );
          const second = await rows();
          const wantSecond = after.versions.map((v) => v.label);
          expect(
            JSON.stringify(second) === JSON.stringify(wantSecond),
            `after the approval the rows read [${second.join(', ')}], not [${wantSecond.join(', ')}]`
          );
          const v1 = after.versions.find((v) => v.label === 'v1');
          const row = page.locator('[data-outcome-row="v1"]');
          const shown = await text(row);
          // innerText carries the state's small-caps transform.
          expect(
            shown.toLowerCase().includes(v1.state),
            `v1's row reads "${shown}", without its state ${v1.state}`
          );
          const said = await text(row.locator('[data-outcome-few]'));
          const want = `Too few items to compare yet (n = ${v1.n}).`;
          expect(said === want, `v1 reads "${said}", not "${want}"`);
          await fits(page, problems, expect);
        },
        size,
        prepare
      );
    });
  }

  await check('/prompts says the outcomes could not be read as a sentence', () =>
    visit(
      '/prompts?role=dispatch',
      async (page) => {
        const failure = page.locator('[data-outcomes-failure]');
        await failure.waitFor({ state: 'visible', timeout: 15_000 });
        const said = await text(failure);
        const want = 'The dispatch outcomes could not be read (500), so no version can be compared here.';
        expect(said === want, `the failure reads "${said}", not "${want}"`);
        expect((await page.locator('[data-outcomes-table]').count()) === 0, 'a table is drawn beside the failure');
      },
      PHONE,
      (page) => serveOutcomes(page, 'dispatch', { status: 500, json: { detail: 'boom' } })
    )
  );

  await check('/prompts never draws an outcomes table no generation names', () =>
    visit(
      '/prompts?role=dispatch',
      async (page) => {
        const failure = page.locator('[data-outcomes-failure]');
        await failure.waitFor({ state: 'visible', timeout: 15_000 });
        const said = await text(failure);
        const want =
          'The dispatch profile changed while its outcomes were being read, so no version can be compared here.';
        expect(said === want, `the failure reads "${said}", not "${want}"`);
        expect((await page.locator('[data-outcomes-table]').count()) === 0, 'a table from two decisions is drawn');
      },
      PHONE,
      (page) => serveOutcomes(page, 'dispatch', { json: { ...SCENARIO.dispatch, generation: null } })
    )
  );

  /**
   * The versions read lands at generation 0 (v1 proposed), an approval commits
   * elsewhere, and the outcomes read lands at generation 1 (v1 active). An
   * observer notes any moment the table is drawn beside the proposed history.
   */
  const splitRead = (moves) => {
    const { after } = SCENARIO.approval;
    const state = { history: 0, preview: 0, outcomes: 0 };
    const prepare = async (page) => {
      await page.addInitScript(() => {
        window.__splitPair = false;
        new MutationObserver(() => {
          const table = document.querySelector('[data-outcomes-table]');
          if (table && document.querySelector('[data-pending="1"]')) window.__splitPair = true;
        }).observe(document, { childList: true, subtree: true });
      });
      await page.route(isPath('/api/prompts/dispatch/versions'), (route) => {
        state.history += 1;
        return route.fulfill({ json: moves && state.history > 1 ? VERSIONS.approved : VERSIONS.proposed });
      });
      await page.route(isPath('/api/prompts/dispatch'), (route) => {
        state.preview += 1;
        return moves && state.preview > 1 ? route.fulfill({ json: VERSIONS.preview_approved }) : route.fallback();
      });
      await page.route(isPath('/api/prompts/dispatch/outcomes'), (route) => {
        state.outcomes += 1;
        return route.fulfill({ json: after });
      });
    };
    return { state, prepare };
  };

  for (const size of [{ width: WIDTH, height: HEIGHT }, PHONE]) {
    await check(
      `/prompts reads again an outcomes table a generation ahead of its versions at ${size.width}px`,
      () => {
        const { state, prepare } = splitRead(true);
        return visit(
          '/prompts?role=dispatch',
          async (page, problems) => {
            await page.locator('[data-outcome-row="v1"]').waitFor({ state: 'visible', timeout: 15_000 });
            await page.locator('[data-active-version="1"]').waitFor({ state: 'visible', timeout: 15_000 });
            expect(state.outcomes >= 2, `the outcomes were read ${state.outcomes} time(s), never again`);
            expect(state.history >= 2, `the versions were read ${state.history} time(s), never again`);
            const split = await page.evaluate(() => window.__splitPair);
            expect(!split, 'the generation-1 table was drawn beside the generation-0 history');
            await fits(page, problems, expect);
          },
          size,
          prepare
        );
      }
    );
  }

  await check('/prompts says so when the outcomes never reach the versions generation', () => {
    const { state, prepare } = splitRead(false);
    return visit(
      '/prompts?role=dispatch',
      async (page) => {
        const said = page.locator('[data-outcomes-diverged]');
        await said.waitFor({ state: 'visible', timeout: 15_000 });
        const want = 'The versions kept changing while this table was read, so no version can be compared here.';
        expect((await text(said)) === want, `the pane reads "${await text(said)}", not "${want}"`);
        expect((await page.locator('[data-outcomes-table]').count()) === 0, 'a table from another generation is drawn');
        const meta = await text(page.locator('[data-profile-outcomes] > :first-child'));
        expect(!/\d/.test(meta), `the section head still counts versions: "${meta}"`);
        expect(state.outcomes === 4, `the outcomes were read ${state.outcomes} times, not 1 and 3 re-reads`);
        const split = await page.evaluate(() => window.__splitPair);
        expect(!split, 'the generation-1 table was drawn beside the generation-0 history');
      },
      PHONE,
      prepare
    );
  });
}
