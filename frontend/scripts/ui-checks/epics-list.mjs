/**
 * The Epics list: the Signal cell and the ticks cell.
 *
 * One surface of `npm run ui-check`; `scripts/ui-check.mjs` runs it (#2570).
 */
// ui-check-owns: src/routes/(factory)/epics/+page.svelte
import { SETTLE_MS } from '../ui-check-lib.mjs';

export async function checks({ base, context, check, visit, expect, sleep }) {
  /**
   * #2540 (D22) — at 1440 the Epics list's Signal cell holds `progressing`
   * and its "no heartbeat" note whole. It was cut to `progressi…` beside
   * ~400px of empty width; read as every text box in the cell inking no more
   * than the box it is drawn in.
   */
  await check('the Epics Signal cell is not ellipsised for progressing at 1440px', async () => {
    const page = await context.newPage();
    try {
      await page.goto(`${base}/epics`, { waitUntil: 'load' });
      const cells = page.locator('[data-signal]').filter({ hasText: 'progressing' });
      await cells.first().waitFor({ state: 'visible', timeout: 15_000 });
      await sleep(SETTLE_MS);
      const read = await cells.evaluateAll((els) =>
        els.map((el) => ({
          text: (el.textContent ?? '').replace(/\s+/g, ' ').trim(),
          cut: [el, ...el.querySelectorAll('*')]
            .filter((node) => node.scrollWidth > node.clientWidth + 0.5)
            .map((node) => `${(node.textContent ?? '').trim()} (${node.scrollWidth} in ${node.clientWidth})`)
        }))
      );
      expect(read.length > 0, 'no Epics row reads progressing');
      for (const cell of read) {
        expect(cell.cut.length === 0, `"${cell.text}" is cut: ${cell.cut.join('; ')}`);
      }
    } finally {
      await page.close();
    }
  });

  /**
   * #2545 D23 — LAST TICK and TICKS read the same tick log: a row whose strip
   * holds ticks never says "no ticks yet" in the cell beside it. The fixture's
   * two active epics have a full strip and no journal tick, which is exactly
   * the row the review found.
   */
  await check('an Epics row with ticks never reads "no ticks yet"', async () => {
    await visit('/epics', async (page) => {
      await page.waitForFunction(
        () =>
          [...document.querySelectorAll('[role="img"][aria-label*="driver ticks"]')].some((strip) =>
            [...strip.children].some((slot) => slot.children.length > 0)
          ),
        null,
        { timeout: 15_000 }
      );
      const rows = await page.evaluate(() =>
        [...document.querySelectorAll('[role="img"][aria-label*="driver ticks"]')]
          .filter((strip) => [...strip.children].some((slot) => slot.children.length > 0))
          .map((strip) => {
            let row = strip.parentElement;
            while (row && !row.querySelector('[data-last-tick]')) row = row.parentElement;
            return {
              label: strip.getAttribute('aria-label').split(':')[0],
              last: row?.querySelector('[data-last-tick]')?.textContent.trim() ?? null
            };
          })
      );
      expect(rows.length > 0, 'no row drew a strip with ticks');
      for (const row of rows) {
        expect(row.last !== null, `${row.label}: no Last tick cell beside the strip`);
        expect(row.last !== 'no ticks yet', `${row.label}: reads "no ticks yet" beside a strip of ticks`);
      }
    });
  });
}
