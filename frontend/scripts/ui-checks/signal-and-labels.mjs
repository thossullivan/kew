/**
 * Signal colours, controls and labels follow DESIGN.md (#2787).
 *
 * - No sentence is set in label-11 (`text-11`, the uppercase tracked mono):
 *   "DATA WAS CHECKED 1W 1D AGO" and "THE REVIEW ROUND CAP WAS REACHED" were
 *   sentences in a face made for labels and status words (D4).
 * - The Monitor's event rows lead with the shared worded name: "Review
 *   outcome", never the capitalised code "Review.outcome" (N8).
 * - An external link is not cyan. Cyan means live, selected, focus or in
 *   review; a link out is text-2 with ↗ (N9).
 *
 * One surface of `npm run ui-check`; `scripts/ui-check.mjs` runs it (#2570).
 */
// ui-check-owns: src/app.css
// ui-check-owns: src/lib/factory/names.ts
// ui-check-owns: src/lib/monitor/EventStream.svelte
// ui-check-owns: src/routes/(factory)/work/+page.*
// ui-check-owns: src/routes/(factory)/health/+page.svelte
// ui-check-owns: src/routes/(factory)/epics/+page.*
// ui-check-owns: src/routes/(factory)/epics/[ref]/+page.*
// ui-check-owns: src/routes/(factory)/runs/[id]/+page.*
// ui-check-owns: src/routes/(factory)/monitor/+page.*
// ui-check-owns: src/routes/(factory)/scorecard/+page.svelte
// ui-check-owns: fixtures/factory/audit.json

const PHONE = { width: 390, height: 844 };

/**
 * The text blocks drawn in label-11, as the reader sees them: every block-level
 * element at or under a `text-11` element whose own children are all inline,
 * read whole. A header row's cells are blockified grid children, so each cell
 * is read on its own and a row of five one-word heads is not "a sentence".
 */
function labelBlocks() {
  const out = [];
  const isBlock = (el) => getComputedStyle(el).display !== 'inline';
  const visible = (el) => {
    const box = el.getBoundingClientRect();
    return box.width > 0 && box.height > 0 && getComputedStyle(el).visibility !== 'hidden';
  };
  const seen = new Set();
  for (const root of document.querySelectorAll('.text-11')) {
    for (const el of [root, ...root.querySelectorAll('*')]) {
      if (seen.has(el) || !visible(el)) continue;
      if (el !== root && !isBlock(el)) continue;
      if ([...el.querySelectorAll('*')].some(isBlock)) continue;
      seen.add(el);
      const text = (el.innerText || '').replace(/\s+/g, ' ').trim();
      if (text) out.push(text);
    }
  }
  return out;
}

/**
 * "More than 4 words or ending in a period" (#2787). A word has a letter or
 * digit. One word is never a sentence, period or not: the Work list's "No."
 * column head is an abbreviation, the label-11 use DESIGN.md names first.
 */
function isSentence(text) {
  const words = text.split(' ').filter((w) => /[\p{L}\p{N}]/u.test(w));
  return words.length > 4 || (words.length > 1 && /\.$/.test(text));
}

const SURFACES = ['/work', '/work?item=SFD-229', '/work?item=2055', '/health', '/epics'];

export async function checks({ check, visit, expect, sleep, sizes: { WIDTH, HEIGHT } }) {
  for (const size of [{ width: WIDTH, height: HEIGHT }, PHONE]) {
    for (const route of SURFACES) {
      await check(`no sentence is set in label-11 on ${route} (${size.width}px)`, () =>
        visit(
          route,
          async (page) => {
            await page.locator('main .text-11').first().waitFor({ state: 'attached', timeout: 15_000 });
            await sleep(500);
            const sentences = (await page.evaluate(labelBlocks)).filter(isSentence);
            expect(
              sentences.length === 0,
              `label-11 carries ${sentences.length} sentence(s):\n` +
                sentences.map((s) => `  "${s}"`).join('\n')
            );
          },
          size
        )
      );
    }
  }

  /**
   * The committed audit fixture carries no `review.outcome`, so this serves the
   * Monitor its own list with one prepended, a second after the newest event:
   * the dotted code is the one the local capitaliser printed as "Review.outcome".
   */
  const withReviewOutcome = (page) =>
    page.route(/\/api\/audit(\?|$)/, async (route) => {
      const response = await route.fetch();
      const body = await response.json();
      const newest = body.events[0];
      const at = new Date(Date.parse(newest.timestamp) + 1000).toISOString().replace('.000Z', '+00:00');
      body.events.unshift({
        ...newest,
        id: newest.id + 1000,
        timestamp: at,
        event: 'review.outcome',
        summary: '{"verdict":"READY"}'
      });
      await route.fulfill({ response, json: body });
    });

  await check('Monitor event rows read "Review outcome", not the code', () =>
    visit(
      '/monitor',
      async (page) => {
        const rows = page.locator('section[aria-label="Event stream"]');
        await rows.waitFor({ state: 'visible', timeout: 15_000 });
        await page
          .getByText('Review outcome', { exact: true })
          .first()
          .waitFor({ state: 'visible', timeout: 15_000 })
          .catch(() => {});
        const text = (await rows.textContent()) ?? '';
        expect(!/Review\.outcome/.test(text), 'an event row reads "Review.outcome"');
        expect(/Review outcome/.test(text), 'no event row reads "Review outcome"');
      },
      undefined,
      withReviewOutcome
    )
  );

  const LINK_ROUTES = ['/work?item=2055', '/epics/launch-fixes?node=2055', '/runs/f5dcb6a5'];
  for (const route of LINK_ROUTES) {
    await check(`an external link on ${route} is not the cyan token`, () =>
      visit(route, async (page) => {
        const links = page.locator('main a[target="_blank"]');
        await links.first().waitFor({ state: 'visible', timeout: 15_000 });
        const read = await page.evaluate(() => {
          const probe = document.createElement('span');
          probe.style.color = 'var(--accent-2)';
          document.body.append(probe);
          const cyan = getComputedStyle(probe).color;
          probe.remove();
          const cyanLinks = [...document.querySelectorAll('main a[target="_blank"]')]
            .filter((a) => a.getBoundingClientRect().width > 0)
            .filter((a) => getComputedStyle(a).color === cyan)
            .map((a) => (a.innerText || '').replace(/\s+/g, ' ').trim());
          return { cyan, cyanLinks };
        });
        expect(
          read.cyanLinks.length === 0,
          `external link(s) drawn in the cyan token ${read.cyan}: ` +
            read.cyanLinks.map((t) => `"${t}"`).join(', ')
        );
      })
    );
  }

  /**
   * D44 (#3470) — Scorecard's offline and empty sentences were set in label-11
   * or in 11px mono, and an empty table's head read "0 models". The reads are
   * routed here — offline (both refused) and live but empty — so the check does
   * not lean on whatever report fixtures the world has.
   */
  const scorecardReads = (mode) => (page) =>
    page.route(
      (url) => url.pathname.startsWith('/api/report/'),
      (route) => {
        if (mode === 'offline') return route.abort('connectionrefused');
        const url = new URL(route.request().url());
        if (url.pathname === '/api/report/models') return route.fulfill({ json: { days: 30, rows: [] } });
        return route.fulfill({ status: 404, json: { detail: 'not served' } });
      }
    );
  for (const size of [PHONE, { width: 768, height: 1024 }, { width: WIDTH, height: HEIGHT }]) {
    for (const mode of ['offline', 'empty']) {
      await check(`/scorecard sets no sentence in 11px type and prints no zero count (${mode}, ${size.width}px)`, () =>
        visit(
          '/scorecard',
          async (page) => {
            await page.locator('main section[aria-label="Cost per model"]').waitFor({ timeout: 15_000 });
            await sleep(300);
            const read = await page.evaluate(() => {
              const visible = (el) => {
                const box = el.getBoundingClientRect();
                return box.width > 0 && box.height > 0;
              };
              const small = [];
              for (const el of document.querySelectorAll('main section *')) {
                if (!visible(el)) continue;
                const own = [...el.childNodes]
                  .filter((n) => n.nodeType === Node.TEXT_NODE)
                  .map((n) => n.textContent)
                  .join(' ')
                  .replace(/\s+/g, ' ')
                  .trim();
                if (!own) continue;
                const style = getComputedStyle(el);
                const mono = /mono/i.test(style.fontFamily);
                if (style.fontSize === '11px' && (mono || style.textTransform === 'uppercase')) small.push(own);
              }
              const heads = [...document.querySelectorAll('main section')].map((s) =>
                (s.firstElementChild?.textContent ?? '').replace(/\s+/g, ' ').trim()
              );
              return {
                small,
                heads,
                scroll: document.documentElement.scrollWidth,
                client: document.documentElement.clientWidth
              };
            });
            const sentences = read.small.filter(isSentence);
            expect(
              sentences.length === 0,
              `/scorecard sets ${sentences.length} sentence(s) in 11px type: ` +
                sentences.map((t) => `"${t}"`).join(', ')
            );
            for (const head of read.heads) {
              expect(!/(^|\D)0 (models?|w\b)|\b0w\b/.test(head), `a /scorecard head reads "${head}"`);
            }
            expect(read.scroll <= read.client, `/scorecard scrolls sideways at ${size.width}px`);
          },
          size,
          scorecardReads(mode)
        )
      );
    }
  }
}
