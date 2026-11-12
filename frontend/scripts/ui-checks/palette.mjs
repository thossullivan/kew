/**
 * The Cmd-K palette (#2516) and an ask over the daily budget (#2676).
 *
 * One surface of `npm run ui-check`; `scripts/ui-check.mjs` runs it (#2570).
 */
// ui-check-owns: src/lib/palette/**
import { SETTLE_MS } from '../ui-check-lib.mjs';

export async function checks({ browser, base, check, expect, sleep, sizes: { WIDTH, HEIGHT } }) {
  /**
   * #2516 — the Cmd-K palette, open, at both widths: it opens on the chord,
   * fits the window (no sideways scroll, the box inside the viewport), and Esc
   * closes it and puts focus back on what held it before. Focus return is a
   * real browser's to judge — jsdom's `focus()` is not a layout's.
   */
  for (const width of [390, WIDTH]) {
    await check(`the palette opens, fits and returns focus on Esc at ${width}px`, async () => {
      const ctx = await browser.newContext({ viewport: { width, height: width === 390 ? 844 : HEIGHT } });
      const page = await ctx.newPage();
      const problems = [];
      page.on('pageerror', (error) => problems.push(`page error: ${error.message}`));
      try {
        await page.goto(`${base}/work`, { waitUntil: 'load' });
        await page.locator('main').first().waitFor({ state: 'visible', timeout: 15_000 });
        await sleep(SETTLE_MS);
        // Something on the page holds focus first, so "returns focus" names a
        // place rather than `body`.
        const opener = page.locator('nav[aria-label="Factory views"] a[href="/monitor"]').first();
        await opener.focus();
        await page.keyboard.press('Control+k');
        const dialog = page.locator('[role="dialog"][aria-label="Go to"]');
        await dialog.waitFor({ state: 'visible', timeout: 5_000 });
        await sleep(SETTLE_MS);

        const read = await page.evaluate(() => {
          const box = document.querySelector('[role="dialog"][aria-label="Go to"]');
          const r = box.getBoundingClientRect();
          return {
            focusInField: document.activeElement === box.querySelector('input'),
            options: box.querySelectorAll('[role="option"]').length,
            left: r.left,
            right: r.right,
            top: r.top,
            bottom: r.bottom,
            vw: document.documentElement.clientWidth,
            vh: window.innerHeight,
            scroll: document.documentElement.scrollWidth
          };
        });
        expect(read.focusInField, `${width}px: the palette opened without focusing its field`);
        expect(read.options > 0, `${width}px: the palette opened with no entries`);
        expect(
          read.left >= 0 && read.right <= read.vw && read.top >= 0 && read.bottom <= read.vh,
          `${width}px: the palette box (${read.left}–${read.right} x ${read.top}–${read.bottom}) ` +
            `leaves the ${read.vw}x${read.vh} window`
        );
        expect(
          read.scroll <= read.vw,
          `${width}px: the page scrolls sideways with the palette open (${read.scroll} > ${read.vw})`
        );

        // Fixture item 2055 has runs of its own (9c41e2b7, f5dcb6a5), each
        // naming 2055 exactly: the item still comes first, its runs after it.
        await page.keyboard.type('2055');
        await sleep(200);
        const rows = await dialog.locator('[role="option"]').evaluateAll((els) =>
          els.map((el) => [...el.querySelectorAll('span')].map((s) => (s.textContent ?? '').trim()))
        );
        const [kind, id] = rows[0] ?? [];
        expect(
          kind === 'item' && id === '2055',
          `${width}px: typing 2055 ranks "${(rows[0] ?? []).join(' ')}" first, not item 2055`
        );
        const runs = rows.filter(([k]) => k === 'run').map(([, i]) => i);
        expect(
          runs.includes('9c41e2b7') && runs.includes('f5dcb6a5'),
          `${width}px: typing 2055 did not also list item 2055's runs (${runs.join(', ') || 'none'})`
        );

        await page.keyboard.press('Escape');
        await dialog.waitFor({ state: 'detached', timeout: 5_000 });
        const back = await opener.evaluate((el) => document.activeElement === el);
        expect(back, `${width}px: Esc closed the palette but did not return focus to the opener`);
        expect(problems.length === 0, problems.join('\n'));
      } finally {
        await page.close();
        await ctx.close();
      }
    });
  }

  /**
   * #2676 — an ask over the daily budget. `/api/meta` is routed to turn ask
   * mode on and `/api/palette/ask` to the server's 429: the palette must show
   * the refusal sentence as text where the results were, and a second Enter
   * must neither ask again nor select anything (no navigation, no confirm
   * step, the dialog still open).
   */
  await check('an ask over the daily budget shows the refusal and Enter does nothing', async () => {
    const SENTENCE =
      "Today's spend ($5.00) has reached the daily budget of $5.00 ([budget] daily_usd), so nothing was asked.";
    const ctx = await browser.newContext({ viewport: { width: WIDTH, height: HEIGHT } });
    const page = await ctx.newPage();
    const problems = [];
    page.on('pageerror', (error) => problems.push(`page error: ${error.message}`));
    let asks = 0;
    try {
      await page.route('**/api/meta', async (route) => {
        const response = await route.fetch();
        const body = await response.json();
        await route.fulfill({ response, json: { ...body, palette_ask: true } });
      });
      await page.route('**/api/palette/ask', async (route) => {
        asks += 1;
        await route.fulfill({ status: 429, json: { detail: SENTENCE } });
      });
      await page.goto(`${base}/work`, { waitUntil: 'load' });
      await page.locator('main').first().waitFor({ state: 'visible', timeout: 15_000 });
      await sleep(SETTLE_MS);
      await page.keyboard.press('Control+k');
      const dialog = page.locator('[role="dialog"][aria-label="Go to"]');
      await dialog.waitFor({ state: 'visible', timeout: 5_000 });
      const field = dialog.locator('input');
      // The placeholder changes once /api/meta has said ask mode is on.
      await page.waitForFunction(
        () => document.querySelector('[role="dialog"] input')?.placeholder.includes('? to ask'),
        null,
        { timeout: 5_000 }
      );
      await field.pressSequentially('what is stuck?');
      await page.keyboard.press('Enter');
      const list = dialog.locator('#palette-results');
      await list.getByText(SENTENCE, { exact: true }).waitFor({ state: 'visible', timeout: 5_000 });
      const before = page.url();
      await page.keyboard.press('Enter');
      await sleep(SETTLE_MS);
      const read = await page.evaluate(() => ({
        options: document.querySelectorAll('[role="dialog"] [role="option"]').length,
        text: (document.querySelector('#palette-results')?.textContent ?? '').trim(),
        confirm: document.querySelector('[role="dialog"] form') !== null,
        open: document.querySelector('[role="dialog"][aria-label="Go to"]') !== null
      }));
      expect(read.text === SENTENCE, `the results list reads "${read.text}", not the refusal sentence`);
      expect(read.options === 0, `the refusal still lists ${read.options} entries to select`);
      expect(asks === 1, `the ask route was called ${asks} times; a second Enter asked again`);
      expect(page.url() === before, `Enter after the refusal navigated to ${page.url()}`);
      expect(!read.confirm, 'Enter after the refusal opened a confirm step');
      expect(read.open, 'Enter after the refusal closed the palette');
      expect(problems.length === 0, problems.join('\n'));
    } finally {
      await page.close();
      await ctx.close();
    }
  });

  /**
   * D45 (#3470) — the palette is a float like a popover (DESIGN.md `popover`):
   * an 11px radius on a `--hair-strong` border. It was a square box on
   * `hair-box`.
   */
  for (const width of [390, 768, WIDTH]) {
    await check(`the open palette has the popover's 11px corners at ${width}px`, async () => {
      const ctx = await browser.newContext({ viewport: { width, height: width === 390 ? 844 : HEIGHT } });
      const page = await ctx.newPage();
      try {
        await page.goto(`${base}/work`, { waitUntil: 'load' });
        await page.locator('main').first().waitFor({ state: 'visible', timeout: 15_000 });
        await sleep(SETTLE_MS);
        await page.keyboard.press('Control+k');
        const dialog = page.locator('[role="dialog"][aria-label="Go to"]');
        await dialog.waitFor({ state: 'visible', timeout: 5_000 });
        const read = await dialog.evaluate((box) => {
          const probe = document.createElement('span');
          probe.style.color = 'var(--hair-strong)';
          document.body.appendChild(probe);
          const hairStrong = getComputedStyle(probe).color;
          probe.remove();
          const style = getComputedStyle(box);
          return { radius: style.borderTopLeftRadius, border: style.borderTopColor, hairStrong };
        });
        expect(read.radius === '11px', `${width}px: the palette's radius is ${read.radius}`);
        expect(
          read.border === read.hairStrong,
          `${width}px: the palette's border is ${read.border}, not --hair-strong (${read.hairStrong})`
        );
      } finally {
        await page.close();
        await ctx.close();
      }
    });
  }
}
