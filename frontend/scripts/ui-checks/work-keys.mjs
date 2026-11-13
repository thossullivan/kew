/**
 * Work's `?` overlay (#2513) and its key legend fold (#2545 D18).
 *
 * One surface of `npm run ui-check`; `scripts/ui-check.mjs` runs it (#2570).
 */
// ui-check-owns: src/routes/(factory)/work/+page.svelte
// ui-check-owns: src/lib/shell/HelpOverlay.svelte
// ui-check-owns: src/lib/keys/registry.ts
// ui-check-owns: src/lib/work/keys.ts
import { SETTLE_MS } from '../ui-check-lib.mjs';

export async function checks({ browser, base, check, visit, expect, sleep, sizes: { WIDTH, HEIGHT } }) {
  /**
   * #2513 — the `?` overlay, on Work, at both widths the console is drawn at.
   *
   * Three readings jsdom cannot make: the box fits the window it opens in
   * (390 included, without scrolling the page sideways); the keys it draws are
   * the ones the LIVE registry holds for Work — read from the page's own
   * module instance, which Vite dev serves at its source path — not a list
   * copied into this script; and Esc in a real browser closes it and puts the
   * focus back on the Work tab it was opened from.
   */
  for (const width of [390, WIDTH]) {
    await check(`the ? overlay lists Work's keys and Esc returns the focus at ${width}px`, async () => {
      const ctx = await browser.newContext({ viewport: { width, height: width === WIDTH ? HEIGHT : 844 } });
      const page = await ctx.newPage();
      const problems = [];
      page.on('pageerror', (error) => problems.push(`page error: ${error.message}`));
      try {
        await page.goto(`${base}/work`, { waitUntil: 'load' });
        const tab = page.locator('nav[aria-label="Factory views"] a[href="/work"]');
        await tab.waitFor({ state: 'visible', timeout: 15_000 });
        await sleep(SETTLE_MS);
        await tab.focus();
        await page.keyboard.press('?');
        const box = page.locator('[role="dialog"][data-help-overlay]');
        await box.waitFor({ state: 'visible', timeout: 5_000 });

        const read = await page.evaluate(async () => {
          const registry = await import('/src/lib/keys/registry.ts');
          const want = registry
            .registeredShortcuts('work')
            .map((s) => registry.keyGlyph(s.key))
            .sort();
          const dialog = document.querySelector('[data-help-overlay]');
          const r = dialog.getBoundingClientRect();
          return {
            want,
            got: [...dialog.querySelectorAll('[data-help-key]')].map((k) => k.textContent).sort(),
            answers: dialog.querySelector('[data-help-answers]')?.textContent ?? '',
            box: { left: r.left, right: r.right, top: r.top, bottom: r.bottom },
            view: { w: window.innerWidth, h: window.innerHeight },
            scroll: document.documentElement.scrollWidth,
            client: document.documentElement.clientWidth
          };
        });
        expect(read.want.length > 0, `${width}px: the registry holds no Work shortcuts`);
        expect(
          JSON.stringify(read.got) === JSON.stringify(read.want),
          `${width}px: the overlay lists [${read.got.join(' ')}], the registry holds [${read.want.join(' ')}]`
        );
        expect(read.answers.includes('What is mine to do'), `${width}px: the Work sentence reads "${read.answers}"`);
        expect(
          read.box.left >= 0 && read.box.right <= read.view.w && read.box.top >= 0,
          `${width}px: the overlay is drawn at ${JSON.stringify(read.box)} in a ${read.view.w}px window`
        );
        expect(
          read.scroll <= read.client,
          `${width}px: the page scrolls sideways with the overlay open — ${read.scroll} over ${read.client}`
        );

        await page.keyboard.press('Escape');
        await box.waitFor({ state: 'detached', timeout: 5_000 });
        await sleep(50);
        const focused = await page.evaluate(() => {
          const el = document.activeElement;
          return el ? `${el.tagName.toLowerCase()} ${el.getAttribute('href') ?? ''}`.trim() : 'nothing';
        });
        expect(focused === 'a /work', `${width}px: after Esc the focus is on ${focused}, not the Work tab`);
        expect(problems.length === 0, problems.join('\n'));
      } finally {
        await page.close();
        await ctx.close();
      }
    });
  }

  /**
   * #2545 D18 — the key legend only where there is a keyboard to read it for
   * (DESIGN.md hides key hints below 1024), and the needs-you headline above
   * the search where the detail pane's queue is not on screen. Both widths, so
   * a legend hidden everywhere fails as surely as one shown on a phone.
   */
  for (const width of [390, WIDTH]) {
    await check(`/work's .keys legend and needs-you strip follow the 1024 fold at ${width}px`, async () => {
      await visit(
        '/work',
        async (page) => {
          await page.locator('aside[aria-label="Issues"] a[data-item]').first().waitFor({ state: 'visible', timeout: 15_000 });
          const legends = page.locator('.keys');
          expect((await legends.count()) > 0, `${width}px: no .keys legend in the page at all`);
          const shown = await legends.evaluateAll((els) =>
            els.filter((el) => el.getBoundingClientRect().height > 0 && getComputedStyle(el).display !== 'none').length
          );
          const strip = page.locator('[data-needs-you-strip]');
          const stripShown = (await strip.count()) > 0 && (await strip.first().isVisible());
          if (width < 1024) {
            expect(shown === 0, `${width}px: the .keys legend is visible on a phone`);
            expect(stripShown, `${width}px: no needs-you strip above the search`);
            const order = await page.evaluate(() => {
              const s = document.querySelector('[data-needs-you-strip]');
              const q = document.querySelector('#work-search');
              return s.getBoundingClientRect().bottom <= q.getBoundingClientRect().top;
            });
            expect(order, `${width}px: the needs-you strip is not above the search`);
            const box = await strip.first().boundingBox();
            expect(box.height <= 48, `${width}px: the needs-you strip is ${box.height}px tall, not one line`);
          } else {
            expect(shown > 0, `${width}px: the .keys legend is hidden on a desktop`);
            expect(!stripShown, `${width}px: the needs-you strip shows beside the queue it repeats`);
          }
        },
        { width, height: width === WIDTH ? HEIGHT : 844 }
      );
    });
  }
}
