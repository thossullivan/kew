/**
 * #3055 — the launchpad drawer says one thing once.
 *
 * N11: the model list's Sonnet 4.6 row said "default" four times, over two
 * labels ("default for default, medium" and "kew's default for default"). It
 * reads one lane line now, `kew default · default, medium tiers`.
 *
 * N13: the drawer's note said "Run hands the command to the drive" under a
 * button that read "Launch" and was off with nothing tying a reason to it. The
 * button now carries the note's verb, and when it is off the sentence beside
 * it says why, named by the button's `aria-describedby`.
 *
 * Both are readings of a real layout and the accessibility tree a person
 * meets, so they are made in a browser: a line count off the laid-out text,
 * and the sentence the button names, visible.
 *
 * One surface of `npm run ui-check`; `scripts/ui-check.mjs` runs it (#2570).
 */
// ui-check-owns: src/lib/work/DispatchLaunchpad.svelte
// ui-check-owns: src/lib/model-picker.ts
// ui-check-owns: src/routes/(factory)/work/+page.*
import { SETTLE_MS } from '../ui-check-lib.mjs';

/** An item the fixtures route to claude-code, so Sonnet 4.6 is in its list. */
const CLAUDE_ITEM = '2050';

/** How often a word stands in `text`, case-blind (the label is set in capitals). */
const count = (text, word) => (text.match(new RegExp(`\\b${word}\\b`, 'gi')) ?? []).length;

async function openDrawer(page, base, item, sleep) {
  await page.goto(`${base}/work?item=${item}`, { waitUntil: 'load' });
  const panel = page.locator('section[aria-label="Plan a dispatch"]');
  await panel.waitFor({ state: 'visible', timeout: 15_000 });
  await sleep(SETTLE_MS);
  await panel.getByRole('button', { name: 'Details' }).click();
  return page.getByRole('dialog', { name: /Plan a dispatch/ });
}

export async function checks({ browser, base, check, expect, sleep, sizes: { WIDTH, HEIGHT } }) {
  await check(`the Sonnet 4.6 row says "default" at most twice, on one line, at ${WIDTH}px`, async () => {
    const ctx = await browser.newContext({ viewport: { width: WIDTH, height: HEIGHT } });
    const page = await ctx.newPage();
    try {
      const drawer = await openDrawer(page, base, CLAUDE_ITEM, sleep);
      const row = drawer.locator('[role="option"][data-model-id="claude-sonnet-4-6"]');
      await row.waitFor({ state: 'attached', timeout: 15_000 });
      await row.scrollIntoViewIfNeeded();
      const read = await row.evaluate((el) => {
        const lane = el.querySelector('[data-lane-line]');
        if (!lane) return { error: 'the row has no lane line' };
        const range = document.createRange();
        range.selectNodeContents(lane);
        // One line box per laid-out line; a line split across two reads as
        // two rects at two different tops.
        const tops = new Set([...range.getClientRects()].map((r) => Math.round(r.top)));
        return { row: el.textContent ?? '', lane: (lane.textContent ?? '').trim(), lines: tops.size };
      });
      expect(read.error === undefined, `${WIDTH}px: ${read.error}`);
      expect(
        read.lane === 'kew default · default, medium tiers',
        `${WIDTH}px: the Sonnet 4.6 lane line reads "${read.lane}"`
      );
      const defaults = count(read.row, 'default');
      expect(
        defaults <= 2,
        `${WIDTH}px: the Sonnet 4.6 row says "default" ${defaults} times: "${read.row.trim()}"`
      );
      expect(read.lines === 1, `${WIDTH}px: the Sonnet 4.6 lane line laid out in ${read.lines} lines`);
    } finally {
      await page.close();
      await ctx.close();
    }
  });

  for (const width of [390, WIDTH]) {
    await check(`the drawer's Run is its note's verb and names why it is off at ${width}px`, async () => {
      const ctx = await browser.newContext({ viewport: { width, height: HEIGHT } });
      const page = await ctx.newPage();
      try {
        // The fleet latch, turned on through the route rather than committed:
        // a stopped fleet in the fixture set would change every other reading
        // of this panel.
        await page.route('**/api/dispatch/resolve*', async (route) => {
          const response = await route.fetch();
          const payload = await response.json();
          payload.admission = { ...payload.admission, fleet_stopped: true, fleet_stopped_reason: '' };
          await route.fulfill({ response, json: payload });
        });
        const drawer = await openDrawer(page, base, '2060', sleep);
        const button = drawer.locator('button[data-drawer-run]');
        await button.waitFor({ state: 'visible', timeout: 15_000 });
        const note = drawer.locator('[data-launch-note]');
        await note.waitFor({ state: 'visible', timeout: 15_000 });

        const label = ((await button.textContent()) ?? '').trim();
        const noteText = ((await note.textContent()) ?? '').trim();
        expect(
          new RegExp(`\\b${label} hands the command\\b`).test(noteText),
          `${width}px: the button reads "${label}", the note says "${noteText}"`
        );
        expect(await button.isDisabled(), `${width}px: Run is on under a stopped fleet`);

        const named = await button.getAttribute('aria-describedby');
        expect(named !== null && named !== '', `${width}px: the off Run names no reason`);
        const reason = page.locator(`[id="${named}"]`);
        expect(await reason.isVisible(), `${width}px: the reason Run names (#${named}) is not visible`);
        const sentence = ((await reason.textContent()) ?? '').trim();
        expect(
          /^Run .+\.$/.test(sentence),
          `${width}px: the reason beside Run is not a sentence about it: "${sentence}"`
        );
        const description = await button.evaluate((el) => {
          const ids = (el.getAttribute('aria-describedby') ?? '').split(/\s+/);
          return ids.map((id) => document.getElementById(id)?.textContent?.trim() ?? '').join(' ');
        });
        expect(description === sentence, `${width}px: Run is described as "${description}"`);

        const box = await button.boundingBox();
        expect(box !== null && box.height >= 32, `${width}px: Run is ${box?.height}px tall, under 32`);
        const sideways = await page.evaluate(
          () => document.documentElement.scrollWidth - document.documentElement.clientWidth
        );
        expect(sideways <= 0, `${width}px: the page scrolls ${sideways}px sideways`);
      } finally {
        await page.close();
        await ctx.close();
      }
    });
  }
}
