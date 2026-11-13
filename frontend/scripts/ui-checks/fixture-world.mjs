/**
 * The fixture world tells one story (#3461, design review c9 D21–D23).
 *
 * One surface of `npm run ui-check`; `scripts/ui-check.mjs` runs it (#2570).
 *
 * The browser checks and the design gallery run on `fixtures/factory/`, so a
 * contradiction there hides real bugs and shows false ones. Three did:
 *
 * - D23. `/scorecard` and `/loops` had no fixture to read: the scorecard drew
 *   "offline · no data" over two 404s and the loops page said "start kew serve
 *   to see loops" while every other page said connected. Each now draws its
 *   data, with no console error (a 404 is one), and nothing drawn past the
 *   window at 390, 768 or 1440 px — which the data showed both pages did:
 *   Scorecard's seven columns and Loops' two side-by-side panes now wrap and
 *   stack below 1024 and 768.
 * - D21. Node #2055 read four ways: the popover's "review stale", the
 *   pipeline's "round 1 · ready", the ledger's "Verdict pending" and a
 *   needs-you merge decision, and the run page named another verdict head.
 *   The story is now round 1 READY at 3670e43f, merge verify failed at that
 *   head: the popover carries no stale review, the pipeline's review stage
 *   and the ledger's verdict both read ready, and the run page's reviewed
 *   head is the head the merge verify and the needs-you sentence name.
 * - D22. Health said the factory could not work (one image missing, a red
 *   "1 down" on every screen) while the header showed a run working. The
 *   header now counts nothing down beside its working run, and Health's
 *   headline says nothing required is down.
 */
// ui-check-owns: src/routes/(factory)/scorecard/**
// ui-check-owns: src/routes/(factory)/loops/**
// ui-check-owns: fixtures/factory/report-models.json
// ui-check-owns: fixtures/factory/report-outcomes.json
// ui-check-owns: fixtures/factory/loops*.json
import { EPIC } from '../ui-check-lib.mjs';

const SIZES = [
  { width: 390, height: 844 },
  { width: 768, height: 1024 },
  { width: 1440, height: 960 }
];

/** The words a page with nothing read says instead of its data. */
const OFFLINE = ['offline · no data', 'start kew serve', 'not read'];

const NODE = '2055';
const RUN = 'f5dcb6a5';
const LOOP_GOAL = '#2071: flaky queue tests';

/** The page's text as drawn, whitespace folded and lowercased (labels are
 * drawn uppercase, and `innerText` reads them as drawn). */
const pageText = (page) =>
  page.evaluate(() => document.body.innerText.replace(/\s+/g, ' ').toLowerCase());

/**
 * What leaves the window sideways: the document's own scroll, and every
 * visible element in `main` drawn past either edge — a pane that clips its
 * overflow hides the scroll and not the problem (Loops' second pane did).
 */
const sideways = (page) =>
  page.evaluate(() => {
    const vw = document.documentElement.clientWidth;
    const off = [];
    for (const el of document.querySelectorAll('main *')) {
      const r = el.getBoundingClientRect();
      if (r.width < 1 || r.height < 1 || el.closest('.sr-only')) continue;
      if (r.left < -0.5 || r.right > vw + 0.5)
        off.push(`${el.tagName.toLowerCase()} "${(el.textContent ?? '').trim().slice(0, 30)}" at x=${Math.round(r.left)}–${Math.round(r.right)}`);
    }
    return { scroll: document.documentElement.scrollWidth - vw, off: off.slice(0, 5) };
  });

/** Wait until `read()` passes `ok`, up to `ms`; the last reading either way. */
async function until(read, ok, sleep, ms = 15_000) {
  let value = await read();
  for (let waited = 0; !ok(value) && waited < ms; waited += 250) {
    await sleep(250);
    value = await read();
  }
  return value;
}

export async function checks({ check, visit, expect, sleep, sizes: { WIDTH, HEIGHT } }) {
  for (const size of SIZES) {
    await check(`/scorecard draws the fixture's model table and outcomes at ${size.width}px`, () =>
      visit(
        '/scorecard',
        async (page, problems) => {
          const text = await until(() => pageText(page), (t) => t.includes('opus'), sleep);
          for (const words of OFFLINE) {
            expect(!text.includes(words), `${size.width}px: /scorecard reads "${words}"`);
          }
          expect(text.includes('kew serve · connected'), `${size.width}px: /scorecard does not say connected`);
          expect(text.includes('opus'), `${size.width}px: /scorecard draws no claude-opus-5 row`);
          expect(problems.length === 0, `${size.width}px: /scorecard logged ${JSON.stringify(problems)}`);
          const over = await sideways(page);
          expect(over.scroll <= 0, `${size.width}px: /scorecard scrolls ${over.scroll}px sideways`);
          expect(over.off.length === 0, `${size.width}px: /scorecard draws past the window: ${over.off.join('; ')}`);
        },
        size
      )
    );

    await check(`/loops draws the fixture's pursuing loop at ${size.width}px`, () =>
      visit(
        '/loops',
        async (page, problems) => {
          const text = await until(() => pageText(page), (t) => t.includes(LOOP_GOAL), sleep);
          for (const words of OFFLINE) {
            expect(!text.includes(words), `${size.width}px: /loops reads "${words}"`);
          }
          expect(!text.includes('no loops yet'), `${size.width}px: /loops reads "no loops yet"`);
          expect(text.includes('kew serve · connected'), `${size.width}px: /loops does not say connected`);
          expect(text.includes(LOOP_GOAL), `${size.width}px: /loops draws no loop for #2071`);
          expect(problems.length === 0, `${size.width}px: /loops logged ${JSON.stringify(problems)}`);
          const over = await sideways(page);
          expect(over.scroll <= 0, `${size.width}px: /loops scrolls ${over.scroll}px sideways`);
          expect(over.off.length === 0, `${size.width}px: /loops draws past the window: ${over.off.join('; ')}`);
        },
        size
      )
    );
  }

  const wide = { width: WIDTH, height: HEIGHT };

  await check(`#${NODE} reads one review state and one verdict head on every surface`, async () => {
    let needsYou = '';
    await visit(
      `${EPIC}?node=${NODE}`,
      async (page) => {
        const why = page.locator('[role="dialog"] .ny .why').first();
        await why.waitFor({ state: 'visible', timeout: 15_000 });
        // The freshness read lands after the popover opens; give it the time
        // a stale badge would take to draw before reading its absence.
        await sleep(1_500);
        const read = await page.evaluate(() => ({
          sentence: document.querySelector('[role="dialog"] .ny .why')?.textContent?.trim() ?? '',
          words: [...document.querySelectorAll('[role="dialog"] .fresh .fb-word')].map(
            (el) => el.textContent?.trim() ?? ''
          )
        }));
        needsYou = read.sentence;
        expect(
          !read.words.includes('review stale'),
          `#${NODE}'s popover reads "review stale" beside a merge decision: ${JSON.stringify(read.words)}`
        );
        expect(/merge decision is yours/.test(read.sentence), `the popover's needs-you reads "${read.sentence}"`);
      },
      wide
    );

    let verifyHead = '';
    await visit(
      `/work?item=${NODE}`,
      async (page) => {
        const read = await until(
          () =>
            page.evaluate(() => {
              const stage = (name, caption) =>
                document
                  .querySelector(`[aria-label="Pipeline stages"] [data-stage="${name}"] [data-caption="${caption}"]`)
                  ?.textContent?.trim() ?? '';
              const verdict = document.querySelector(
                'section[aria-label="Evidence ledger"] li[data-event="adversarial_review_verdict"]'
              );
              return {
                review: `${stage('review', 'primary')} · ${stage('review', 'secondary')}`,
                summary: verdict?.querySelector('[data-summary]')?.textContent?.trim() ?? '',
                mark: verdict?.querySelector('[aria-label]')?.getAttribute('aria-label') ?? '',
                head: document.querySelector('[data-merge-verify-head]')?.textContent?.trim() ?? ''
              };
            }),
          (r) => r.summary !== '' && r.head !== '',
          sleep
        );
        verifyHead = read.head;
        expect(read.review === 'round 1 · ready', `the pipeline's review stage reads "${read.review}"`);
        // The ledger prints each row as `event · summary`.
        expect(
          read.summary === 'adversarial_review_verdict · ready',
          `the ledger's verdict reads "${read.summary}", not "adversarial_review_verdict · ready"`
        );
        expect(!/pending/i.test(read.mark), `the ledger's verdict mark reads "${read.mark}"`);
      },
      wide
    );

    await visit(
      `/runs/${RUN}`,
      async (page) => {
        const head = await until(
          () =>
            page.evaluate(() => {
              const dt = [...document.querySelectorAll('dt')].find((el) => el.textContent?.trim() === 'reviewed head');
              return dt?.nextElementSibling?.getAttribute('title') ?? '';
            }),
          (h) => h !== '',
          sleep
        );
        expect(verifyHead !== '', 'Work drew no merge verify head to compare against');
        expect(
          head.startsWith(verifyHead),
          `the run page's reviewed head ${head} is not the merge verify's ${verifyHead}`
        );
        expect(
          needsYou.includes(`at ${verifyHead}`),
          `the needs-you sentence "${needsYou}" names another head than ${verifyHead}`
        );
      },
      wide
    );
  });

  for (const route of ['/work', EPIC, '/health']) {
    await check(`the header counts nothing down beside its working run on ${route}`, () =>
      visit(
        route,
        async (page) => {
          const read = await until(
            () =>
              page.evaluate(() => ({
                working: (document.querySelector('header')?.innerText ?? '').match(/(\d+)\s*working/)?.[1] ?? '',
                down: [...document.querySelectorAll('header a[href="/health"]')]
                  .map((a) => (a.textContent ?? '').replace(/\s+/g, ' ').trim())
                  .filter((t) => /\bdown\b/.test(t)),
                headline: (document.querySelector('main header p.text-15')?.textContent ?? '').trim()
              })),
            (r) => r.working !== '',
            sleep
          );
          expect(Number(read.working) > 0, `the header reads "${read.working}" working`);
          expect(read.down.length === 0, `the header reads ${JSON.stringify(read.down)} beside a working run`);
          if (route === '/health') {
            expect(
              read.headline.startsWith('Nothing required is down'),
              `Health's headline reads "${read.headline}" while the header shows a run working`
            );
          }
        },
        wide
      )
    );
  }
}
