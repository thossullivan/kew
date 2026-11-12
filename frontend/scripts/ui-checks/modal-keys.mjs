/**
 * Keys behind an open modal, and the Monitor brush's keys in the `?` overlay
 * (#2781).
 *
 * One surface of `npm run ui-check`; `scripts/ui-check.mjs` runs it (#2570).
 */
// ui-check-owns: src/lib/palette/Palette.svelte
// ui-check-owns: src/lib/keys/registry.ts
// ui-check-owns: src/lib/shell/HelpOverlay.svelte
// ui-check-owns: src/lib/monitor/OverviewTrack.svelte
// ui-check-owns: src/routes/(factory)/monitor/+page.*
// ui-check-owns: src/routes/(factory)/work/+page.*
import { SETTLE_MS } from '../ui-check-lib.mjs';

/** The item `?item=` names, or null. */
const itemOf = (url) => new URL(url).searchParams.get('item');

export async function checks({ browser, base, check, expect, sleep, sizes: { WIDTH, HEIGHT } }) {
  /**
   * K1 — with the palette at its confirm step, a `j` from the Confirm button
   * used to walk the Work list behind the box: the step stops only Esc. The
   * key layer is suspended while the palette is open, so `?item=` holds. The
   * same `j` after the palette closes must still move it, or the first half
   * proves nothing.
   */
  for (const width of [390, WIDTH]) {
    await check(`a j at the palette's confirm step leaves ?item= alone at ${width}px`, async () => {
      const ctx = await browser.newContext({ viewport: { width, height: width === WIDTH ? HEIGHT : 844 } });
      const page = await ctx.newPage();
      const problems = [];
      page.on('pageerror', (error) => problems.push(`page error: ${error.message}`));
      try {
        await page.goto(`${base}/work?item=2055`, { waitUntil: 'load' });
        await page.locator('main').first().waitFor({ state: 'visible', timeout: 15_000 });
        await sleep(SETTLE_MS);
        expect(itemOf(page.url()) === '2055', `${width}px: /work opened on ?item=${itemOf(page.url())}`);

        await page.keyboard.press('Control+k');
        const dialog = page.locator('[role="dialog"][aria-label="Go to"]');
        await dialog.waitFor({ state: 'visible', timeout: 5_000 });
        await page.keyboard.type('Stop the fleet');
        await sleep(200);
        await page.keyboard.press('Enter');
        const confirm = page.locator('#palette-confirm');
        await confirm.waitFor({ state: 'visible', timeout: 5_000 });
        // The step focuses its reason field when one is owed, and Confirm is
        // disabled until it is filled. The button is where a `j` is not typed,
        // so it is where the press is made.
        const reason = page.locator('#palette-reason');
        if (await reason.isVisible()) await reason.fill('ui-check');
        await confirm.focus();
        const onConfirm = await confirm.evaluate((el) => document.activeElement === el);
        expect(onConfirm, `${width}px: the Confirm button could not take the focus`);
        await page.keyboard.press('j');
        await sleep(300);
        expect(
          itemOf(page.url()) === '2055',
          `${width}px: j at the confirm step moved ?item= to ${itemOf(page.url())} behind the palette`
        );
        expect(await confirm.isVisible(), `${width}px: j at the confirm step left it`);

        // Esc leaves the step, Esc again closes the palette: the page answers.
        await page.keyboard.press('Escape');
        await page.keyboard.press('Escape');
        await dialog.waitFor({ state: 'detached', timeout: 5_000 });
        await page.evaluate(() => (document.activeElement instanceof HTMLElement ? document.activeElement.blur() : null));
        await page.keyboard.press('j');
        await sleep(300);
        expect(
          itemOf(page.url()) !== '2055',
          `${width}px: j after the palette closed did not move ?item= either, so the check proves nothing`
        );
        expect(problems.length === 0, problems.join('\n'));
      } finally {
        await page.close();
        await ctx.close();
      }
    });
  }

  /**
   * K5/K6 — the brush's keys are on the key layer: `?` on /monitor lists the
   * ones the live registry holds for the brush (read from the page's own
   * module instance, as work-keys.mjs does), and ⌃= on the focused track is
   * left to the browser while a bare `=` zooms.
   */
  for (const width of [390, WIDTH]) {
    await check(`? on /monitor lists the brush keys, and ^= is not the brush's at ${width}px`, async () => {
      const ctx = await browser.newContext({ viewport: { width, height: width === WIDTH ? HEIGHT : 844 } });
      const page = await ctx.newPage();
      const problems = [];
      page.on('pageerror', (error) => problems.push(`page error: ${error.message}`));
      try {
        await page.goto(`${base}/monitor`, { waitUntil: 'load' });
        const track = page.locator('[role="slider"][aria-label^="Event overview"]');
        await track.waitFor({ state: 'visible', timeout: 15_000 });
        await sleep(SETTLE_MS);

        await track.focus();
        const before = await track.getAttribute('aria-valuetext');
        await page.keyboard.press('Control+Equal');
        await sleep(300);
        const chorded = await track.getAttribute('aria-valuetext');
        expect(chorded === before, `${width}px: ^= moved the brush from "${before}" to "${chorded}"`);
        await page.keyboard.press('Equal');
        await sleep(300);
        const zoomed = await track.getAttribute('aria-valuetext');
        expect(zoomed !== before, `${width}px: a bare = left the brush at "${before}", so ^= proves nothing`);

        await page.keyboard.press('?');
        const box = page.locator('[role="dialog"][data-help-overlay]');
        await box.waitFor({ state: 'visible', timeout: 5_000 });
        const read = await page.evaluate(async () => {
          const registry = await import('/src/lib/keys/registry.ts');
          const dialog = document.querySelector('[data-help-overlay]');
          return {
            want: registry.registeredShortcuts('brush').map((s) => registry.keyGlyph(s.key)),
            got: [...dialog.querySelectorAll('[data-help-key]')].map((k) => k.textContent),
            text: dialog.textContent ?? ''
          };
        });
        expect(read.want.length > 0, `${width}px: the registry holds no brush shortcuts on /monitor`);
        const missing = read.want.filter((k) => !read.got.includes(k));
        expect(
          missing.length === 0,
          `${width}px: the overlay lists [${read.got.join(' ')}], missing the brush's [${missing.join(' ')}]`
        );
        expect(
          !read.text.includes('no shortcuts of its own'),
          `${width}px: the overlay still says /monitor has no shortcuts of its own`
        );
        await page.keyboard.press('Escape');
        await box.waitFor({ state: 'detached', timeout: 5_000 });
        expect(problems.length === 0, problems.join('\n'));
      } finally {
        await page.close();
        await ctx.close();
      }
    });
  }
}
