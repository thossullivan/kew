/**
 * The launchpad's fix links (#2522).
 *
 * One surface of `npm run ui-check`; `scripts/ui-check.mjs` runs it (#2570).
 */
// ui-check-owns: src/lib/work/DispatchLaunchpad.svelte
// ui-check-owns: src/lib/work/launchpad.ts
// ui-check-owns: src/routes/(factory)/work/+page.*
import { SETTLE_MS } from '../ui-check-lib.mjs';

export async function checks({ browser, base, check, expect, sleep, sizes: { WIDTH, HEIGHT } }) {
  /**
   * #2522 — the launchpad's two fix links, at phone and desktop width.
   *
   * An invalid row carries the place that fixes it, and both printings of that
   * link were laid out by a box that could not hold it. On the row, the cell
   * was one wrapping line inside a FIXED 50px row: the link wrapped to a
   * second line the row had no height for and was painted over the IMAGE
   * row's value underneath. In the What-needs-fixing band it was a flex
   * sibling of the refusal sentence, so the sentence took the width and the
   * link was squeezed into ~60px, one word per line, beside a three-line
   * remedy.
   *
   * Neither is reachable from jsdom — both are a wrap decision a real layout
   * makes — so the readings are geometric: the row link's box against its OWN
   * row's box, and, for "one line", the number of line boxes the words inside
   * a link actually laid out in.
   *
   * The fixtures resolve a clean route, so the refusal is turned on through
   * the route rather than committed: a refused posture in the fixture set
   * would change every artboard comparison of this panel.
   */
  const REFUSAL =
    "cannot resolve image 'kew-codex:latest' to an immutable image ID: no container " +
    'runtime is available — this dispatch is refused. Install Docker/OrbStack, or set ' +
    '[sandbox] enabled = false with dangerously_skip_containment = true to run uncontained.';

  for (const width of [390, WIDTH]) {
    await check(`the launchpad keeps its fix links in their own box at ${width}px`, async () => {
      const ctx = await browser.newContext({ viewport: { width, height: HEIGHT } });
      const page = await ctx.newPage();
      try {
        await page.route('**/api/dispatch/resolve*', async (route) => {
          const response = await route.fetch();
          const payload = await response.json();
          payload.containment = {
            ...payload.containment,
            posture: 'refused',
            refusal_reason: REFUSAL
          };
          await route.fulfill({ response, json: payload });
        });

        await page.goto(`${base}/work?item=2060`, { waitUntil: 'load' });
        const panel = page.locator('section[aria-label="Plan a dispatch"]');
        await panel.waitFor({ state: 'visible', timeout: 15_000 });
        await sleep(SETTLE_MS);
        // The rows and the band are in the drawer (#2535), and the image row
        // is a folded one there (a refused route resolved none), so both the
        // drawer and "N more fields ▸" are pressed open first.
        await panel.locator('button').filter({ hasText: 'Details' }).first().click();
        await page
          .locator('section[aria-label="What needs fixing"]')
          .waitFor({ state: 'visible', timeout: 15_000 });
        await panel.locator('button').filter({ hasText: 'more fields ▸' }).first().click();
        await sleep(SETTLE_MS);

        const read = await page.evaluate(() => {
          const box = (el) => {
            const r = el.getBoundingClientRect();
            return { top: r.top, bottom: r.bottom, left: r.left, right: r.right, h: r.height };
          };
          /**
           * The line boxes the element's own text laid out in. A Range over the
           * contents, not `el.getClientRects()`: both of these links are flex
           * or grid items, which are blockified, so their element box is ONE
           * rect however many lines the words inside it took. The range reports
           * the lines.
           */
          const lines = (el) => {
            const range = document.createRange();
            range.selectNodeContents(el);
            return range.getClientRects().length;
          };
          /** The element's own line box, which the two links do not share. */
          const lineHeight = (el) => parseFloat(getComputedStyle(el).lineHeight) || 0;
          const panel = document.querySelector('section[aria-label="Plan a dispatch"]');
          if (!panel) return { error: 'no launchpad panel on the page' };
          // Every row of the table is the grid this panel sets inline, so the
          // rows are found by that and named by their own field column.
          const rows = [...panel.querySelectorAll('div[style*="grid-template-columns"]')];
          const rowFor = (field) =>
            rows.find((row) => (row.firstElementChild?.textContent ?? '').trim() === field);
          const posture = rowFor('posture');
          const image = rowFor('image');
          if (!posture || !image) return { error: `rows found: ${rows.length}, no posture/image` };
          const rowLink = posture.querySelector('a[href="/health#posture"]');
          const band = document.querySelector('section[aria-label="What needs fixing"]');
          const bandLink = band?.querySelector('a[href="/health#posture"]');
          if (!rowLink || !bandLink) return { error: 'the posture fix link is not on screen' };
          return {
            rowBox: box(posture),
            imageBox: box(image),
            rowLink: { ...box(rowLink), lh: lineHeight(rowLink) },
            bandLink: {
              ...box(bandLink),
              lines: lines(bandLink),
              lh: lineHeight(bandLink),
              text: (bandLink.textContent ?? '').trim()
            }
          };
        });
        expect(read.error === undefined, `${width}px: ${read.error}`);

        // 1. The link belongs to its row: its box is inside the POSTURE row's,
        //    which is the same reading as "not on top of the IMAGE row".
        expect(
          read.rowLink.top >= read.rowBox.top - 0.5 &&
            read.rowLink.bottom <= read.rowBox.bottom + 0.5,
          `${width}px: the posture row's fix link spans ` +
            `${read.rowLink.top.toFixed(1)}–${read.rowLink.bottom.toFixed(1)}px, outside its row's ` +
            `${read.rowBox.top.toFixed(1)}–${read.rowBox.bottom.toFixed(1)}px — the row did not grow`
        );
        expect(
          read.rowLink.bottom <= read.imageBox.top + 0.5,
          `${width}px: the posture fix link reaches ${read.rowLink.bottom.toFixed(1)}px, ` +
            `into the image row that starts at ${read.imageBox.top.toFixed(1)}px`
        );
        // 2. And the line it was given holds it: a one-line box, not a stack
        //    of words. The BOX, not the range, because the label is truncated
        //    there — a clipped label still lays its words out past its box, and
        //    being cut inside the row is this line's own last defence.
        expect(
          read.rowLink.h <= read.rowLink.lh * 1.5,
          `${width}px: the posture row's fix link is ${read.rowLink.h.toFixed(1)}px tall ` +
            `over a ${read.rowLink.lh.toFixed(1)}px line — it is wrapping, not one line`
        );

        // 3. The row only GREW; the floor is still a floor. A row with no fix
        //    on it measures the 50px the artboard draws (the image row, whose
        //    value and source fit one line at this width) — a row that grew
        //    because padding was added to every row would miss it.
        if (width === WIDTH)
          expect(
            Math.abs(read.imageBox.h - 50) <= 0.5,
            `${width}px: the image row is ${read.imageBox.h.toFixed(1)}px, not the ` +
              "table's 50px — the fix line grew every row, not the one carrying it"
          );

        // 4. The band's link, one line: it follows the remedy sentence rather
        //    than sharing the width with it.
        expect(
          read.bandLink.text.startsWith('Health · posture by lane'),
          `${width}px: the band's link reads "${read.bandLink.text}"`
        );
        expect(
          read.bandLink.lines === 1,
          `${width}px: the band's fix link laid out in ${read.bandLink.lines} lines ` +
            `(box ${read.bandLink.h.toFixed(1)}px over a ${read.bandLink.lh.toFixed(1)}px line)`
        );

        // Nothing gained sideways on the way: the page must not scroll to it.
        const pageBox = await page.evaluate(() => ({
          scroll: document.documentElement.scrollWidth,
          client: document.documentElement.clientWidth
        }));
        expect(
          pageBox.scroll <= pageBox.client,
          `${width}px: the page scrolls sideways — scrollWidth ${pageBox.scroll} ` +
            `over clientWidth ${pageBox.client}`
        );
      } finally {
        await page.close();
        await ctx.close();
      }
    });
  }
}
