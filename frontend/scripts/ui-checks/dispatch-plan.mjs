/**
 * "Plan a dispatch", disclosed progressively (#2535).
 *
 * One surface of `npm run ui-check`; `scripts/ui-check.mjs` runs it (#2570).
 */
// ui-check-owns: src/lib/work/DispatchLaunchpad.svelte
// ui-check-owns: src/routes/(factory)/work/+page.*
import { SETTLE_MS } from '../ui-check-lib.mjs';

export async function checks({ browser, base, check, expect, sleep, sizes: { WIDTH, HEIGHT } }) {
  /**
   * #2535 — "Plan a dispatch", disclosed progressively, at phone and desktop
   * width. Closed, it is ONE line: the route, the verdict and the primary
   * action, read as the summary box being one line box tall and its text one
   * line box long. Opened, it is a drawer that fits the viewport; closed
   * again, it is gone. Neither state may make the page scroll sideways.
   */
  for (const width of [390, WIDTH]) {
    await check(`the dispatch plan is one line closed and a drawer open at ${width}px`, async () => {
      const ctx = await browser.newContext({ viewport: { width, height: HEIGHT } });
      const page = await ctx.newPage();
      const sideways = () =>
        page.evaluate(() => ({
          scroll: document.documentElement.scrollWidth,
          client: document.documentElement.clientWidth
        }));
      try {
        await page.goto(`${base}/work?item=2060`, { waitUntil: 'load' });
        const panel = page.locator('section[aria-label="Plan a dispatch"]');
        await panel.waitFor({ state: 'visible', timeout: 15_000 });
        const line = panel.locator('[data-plan-summary]');
        // The closed line is a route once the plan has landed.
        await line.filter({ hasText: ' / ' }).waitFor({ state: 'visible', timeout: 15_000 });
        await sleep(SETTLE_MS);

        const closed = await line.evaluate((el) => {
          const route = el.querySelector('span');
          const range = document.createRange();
          range.selectNodeContents(route);
          const lineTops = new Set(
            [...range.getClientRects()].map((r) => Math.round(r.top))
          );
          const buttons = [...el.querySelectorAll('button')].map((b) => b.getBoundingClientRect());
          const box = el.getBoundingClientRect();
          return {
            text: (el.textContent ?? '').replace(/\s+/g, ' ').trim(),
            h: box.height,
            right: box.right,
            routeLines: lineTops.size,
            tallest: Math.max(...buttons.map((b) => b.height)),
            buttonTops: buttons.map((b) => Math.round(b.top)),
            dialogs: document.querySelectorAll('[role="dialog"]').length
          };
        });
        expect(closed.dialogs === 0, `${width}px: a dialog is open before Details was pressed`);
        expect(
          /\bready\b|\brefused\b/.test(closed.text),
          `${width}px: the closed line carries no verdict: "${closed.text}"`
        );
        expect(
          closed.routeLines === 1,
          `${width}px: the closed line's route laid out in ${closed.routeLines} lines`
        );
        // One line: no taller than its tallest control plus the line's own
        // padding, and every control on the same row.
        expect(
          closed.h <= closed.tallest + 24 + 0.5,
          `${width}px: the closed line is ${closed.h.toFixed(1)}px tall over a ` +
            `${closed.tallest.toFixed(1)}px control — it wrapped`
        );
        expect(
          new Set(closed.buttonTops).size === 1,
          `${width}px: the closed line's actions sit on ${new Set(closed.buttonTops).size} rows`
        );
        expect(closed.right <= width + 0.5, `${width}px: the closed line ends at ${closed.right}px`);
        let box = await sideways();
        expect(
          box.scroll <= box.client,
          `${width}px closed: the page scrolls sideways — ${box.scroll} over ${box.client}`
        );

        // Open: a drawer inside the viewport, holding the inputs and the route.
        await line.locator('button').filter({ hasText: 'Details' }).click();
        const dialog = page.locator('[role="dialog"][aria-modal="true"]');
        await dialog.waitFor({ state: 'visible', timeout: 15_000 });
        await sleep(SETTLE_MS);
        const opened = await dialog.evaluate((el) => {
          const r = el.getBoundingClientRect();
          return {
            left: r.left,
            right: r.right,
            scroll: el.scrollWidth,
            client: el.clientWidth,
            inputs: el.querySelectorAll('#plan-complexity, #plan-lane').length,
            model: (el.querySelector('[data-route="model"]')?.textContent ?? '').trim()
          };
        });
        expect(
          opened.left >= -0.5 && opened.right <= width + 0.5,
          `${width}px: the drawer spans ${opened.left.toFixed(1)}–${opened.right.toFixed(1)}px`
        );
        expect(
          opened.scroll <= opened.client,
          `${width}px: the drawer scrolls sideways — ${opened.scroll} over ${opened.client}`
        );
        expect(opened.inputs === 2, `${width}px: the drawer holds ${opened.inputs} of 2 inputs`);
        expect(opened.model !== '', `${width}px: the drawer prints no model`);
        box = await sideways();
        expect(
          box.scroll <= box.client,
          `${width}px open: the page scrolls sideways — ${box.scroll} over ${box.client}`
        );

        // Close: Escape, and the drawer is gone — and the selection it was
        // about is not (the page's own Escape clears the item).
        await page.keyboard.press('Escape');
        await dialog.waitFor({ state: 'detached', timeout: 15_000 });
        expect(
          (await page.locator('section[aria-label="Plan a dispatch"]').count()) === 1,
          `${width}px: Escape closed the item along with the drawer`
        );
        box = await sideways();
        expect(
          box.scroll <= box.client,
          `${width}px reclosed: the page scrolls sideways — ${box.scroll} over ${box.client}`
        );
      } finally {
        await page.close();
        await ctx.close();
      }
    });
  }
}
