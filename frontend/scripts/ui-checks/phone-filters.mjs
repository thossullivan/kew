/**
 * The run-event filters at 390, and the palette field as a control (#2850,
 * design-c6 P18 P20).
 *
 * One surface of `npm run ui-check`; `scripts/ui-check.mjs` runs it (#2570).
 */
// ui-check-owns: src/lib/runs/RunEvents.svelte
// ui-check-owns: src/lib/palette/Palette.svelte
import { SETTLE_MS } from '../ui-check-lib.mjs';

const PHONE = { width: 390, height: 844 };

export async function checks({ browser, base, check, visit, expect, sleep, sizes: { WIDTH, HEIGHT } }) {
  /**
   * P20 — the eleven kind chips and the presets wrapped into about 400px of
   * pills before the first event at 390. Below 640 they scroll in one row, so
   * the list starts within 200px of the row's top. The row is read as the
   * parent of the first kind chip, and the event as the list's first row, so a
   * row hidden or an empty list cannot pass by measuring nothing.
   */
  await check('the first run event starts within 200px of the chip row at 390px', () =>
    visit(
      '/runs/f5dcb6a5',
      async (page) => {
        const section = page.locator('section[aria-label="Run events"]');
        await section.locator('button[aria-pressed]').first().waitFor({ state: 'visible', timeout: 15_000 });
        await section.locator('[aria-label="Ledger events"] [role="listitem"]').first().waitFor({
          state: 'visible',
          timeout: 15_000
        });
        const read = await section.evaluate((el) => {
          const chips = [...el.querySelectorAll('button[aria-pressed]')];
          const row = chips[0].parentElement;
          const first = el.querySelector('[aria-label="Ledger events"] [role="listitem"]');
          return {
            chips: chips.length,
            rowTop: row.getBoundingClientRect().top,
            eventTop: first.getBoundingClientRect().top,
            viewport: document.documentElement.clientWidth,
            scroll: document.documentElement.scrollWidth
          };
        });
        expect(read.chips >= 5, `the run drew ${read.chips} kind chips, too few to prove the row`);
        const gap = read.eventTop - read.rowTop;
        expect(gap <= 200, `the first event starts ${gap.toFixed(1)}px below the chip row's top`);
        expect(
          read.scroll <= read.viewport,
          `the chip row pushes the page sideways (${read.scroll} > ${read.viewport})`
        );
      },
      PHONE
    )
  );

  /**
   * P18 — the palette field read as a 22px line with no caret or ring when the
   * palette opened. It is at least 32px tall and draws the cyan ring
   * (`--accent-2`, DESIGN.md: cyan is focus) the moment the chord opens it, at
   * both widths. The ring is read off the field's computed outline, compared
   * with the token as the same page resolves it.
   */
  for (const width of [390, WIDTH]) {
    await check(`the palette field is 32px or taller with the focus ring at ${width}px`, async () => {
      const ctx = await browser.newContext({ viewport: { width, height: width === 390 ? 844 : HEIGHT } });
      const page = await ctx.newPage();
      try {
        await page.goto(`${base}/work`, { waitUntil: 'load' });
        await page.locator('main').first().waitFor({ state: 'visible', timeout: 15_000 });
        await sleep(SETTLE_MS);
        await page.keyboard.press('Control+k');
        const field = page.locator('[role="dialog"][aria-label="Go to"] input[role="combobox"]');
        await field.waitFor({ state: 'visible', timeout: 5_000 });
        await sleep(200);
        const read = await field.evaluate((el) => {
          const style = getComputedStyle(el);
          const probe = document.createElement('span');
          probe.style.color = 'var(--accent-2)';
          document.body.append(probe);
          const cyan = getComputedStyle(probe).color;
          probe.remove();
          return {
            focused: document.activeElement === el,
            height: el.getBoundingClientRect().height,
            outlineStyle: style.outlineStyle,
            outlineWidth: parseFloat(style.outlineWidth),
            outlineColor: style.outlineColor,
            cyan
          };
        });
        expect(read.focused, `${width}px: the palette opened without focusing its field`);
        expect(read.height >= 32 - 0.5, `${width}px: the palette field is ${read.height.toFixed(1)}px tall`);
        expect(
          read.outlineStyle !== 'none' && read.outlineWidth >= 1,
          `${width}px: the focused field draws no ring (outline ${read.outlineStyle} ${read.outlineWidth}px)`
        );
        expect(
          read.outlineColor === read.cyan,
          `${width}px: the field's ring is ${read.outlineColor}, not the cyan ${read.cyan}`
        );
      } finally {
        await page.close();
        await ctx.close();
      }
    });
  }
}
