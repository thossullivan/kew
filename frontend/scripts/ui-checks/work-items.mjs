/**
 * Opening a Work item, and an item with no runs.
 *
 * One surface of `npm run ui-check`; `scripts/ui-check.mjs` runs it (#2570).
 */
// ui-check-owns: src/routes/(factory)/work/+page.svelte
// ui-check-owns: src/lib/work/Pipeline.svelte
import { SETTLE_MS } from '../ui-check-lib.mjs';

export async function checks({ base, check, visit, expect, sleep }) {
  await check('a Work row click opens that item', () =>
    visit('/work', async (page) => {
      const rows = page.locator('aside[aria-label="Issues"] a[href*="item="]');
      await rows.first().waitFor({ state: 'visible', timeout: 15_000 });
      const row = rows.first();
      const item = new URL(await row.getAttribute('href'), base).searchParams.get('item');
      // The row's three cells, in order: number, title, state.
      const title = ((await row.locator('> span').nth(1).textContent()) ?? '').trim();
      expect(title.length > 0, 'the first row has no title to look for');
      expect(!page.url().includes('item='), `/work opened with ?item= already set (${page.url()})`);
      const pane = page.locator('section[aria-label="Item detail"]');
      const before = (await pane.textContent()) ?? '';
      await row.click();
      await sleep(400);
      const chosen = new URL(page.url()).searchParams.get('item');
      expect(chosen === item, `the row click left ?item= at ${chosen}, not ${item}`);
      expect(
        (await row.getAttribute('aria-current')) === 'true',
        'the clicked row is not aria-current'
      );
      const after = (await pane.textContent()) ?? '';
      expect(after !== before, 'the detail pane did not change at all');
      expect(after.includes(title), `the pane does not show "${title}":\n${after.slice(0, 400)}`);
    })
  );

  /**
   * #2572 — an idle ready-queue row nothing has run for. Its pipeline read used
   * to answer 404, which the browser logs as a console error on every such
   * selection, so no "loads clean" check could be run against live data. The
   * row is selected by a click, the way an operator does, and the console is
   * judged after the reads it starts have landed.
   */
  await check('selecting a Work item with no runs is quiet and says "No run yet"', () =>
    visit('/work', async (page, problems) => {
      const row = page.locator('aside[aria-label="Issues"] a[href*="item=2060"]').first();
      await row.waitFor({ state: 'visible', timeout: 15_000 });
      await row.click();
      const pipeline = page.locator('section[aria-label="Pipeline"]');
      await pipeline.waitFor({ state: 'visible', timeout: 15_000 });
      await sleep(SETTLE_MS);
      const text = (await pipeline.textContent()) ?? '';
      expect(text.includes('No run yet'), `the pipeline does not say "No run yet":\n${text}`);
      expect(problems.length === 0, `selecting #2060 reported:\n${problems.join('\n')}`);
    })
  );
}
