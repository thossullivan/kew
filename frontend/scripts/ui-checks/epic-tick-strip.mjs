/**
 * The epic page's tick strip at phone width (#2473).
 *
 * One surface of `npm run ui-check`; `scripts/ui-check.mjs` runs it (#2570).
 */
// ui-check-owns: src/lib/factory/tick-strip.ts
// ui-check-owns: src/lib/factory/TickTimeline.svelte
// ui-check-owns: src/routes/(factory)/epics/[ref]/**
// ui-check-owns: fixtures/factory/epics-launch-fixes*.json
import { EPIC, SETTLE_MS } from '../ui-check-lib.mjs';

export async function checks({ browser, base, check, expect, sleep, sizes: { WIDTH } }) {
  /**
   * #2473 — the epic page's full tick strip at phone width. The strip drew at
   * its design 760px inside a box this page clips at the viewport (the epic
   * column's `overflow-x-hidden`), so at 390px about half of it was cut off.
   * Which half is the whole point: the strip places its newest attempt LAST
   * (`tick-strip.ts`), so what the clip took was the recent ticks — the part
   * somebody checking "is it still moving" came for — while the oldest ones kept
   * their pixels. The page does not scroll sideways, so there was no way to
   * reach them.
   *
   * No vitest run can see this: the strip's own geometry was correct at the
   * width it was told, and the width it was told was the only thing wrong.
   * `fitStripWidth` hands it the box instead; this reads the result against the
   * box that actually clips it, at 390 AND at 1440, because a fix that fitted
   * the phone by shrinking the desktop strip would be a different bug.
   *
   * It also reads how many BARS came out of it, and that needs density the
   * committed fixture does not have (24 attempts, which any width here can draw
   * apart). Fitting the width alone left the page asking for the 189 attempts
   * the 760px lane is sized for and handing all of them to a 310px one, where
   * the batcher's 4px floor merged the lot into a single bar — a strip that fits
   * its box perfectly and says nothing (review round 1). So the attempts are
   * served through this check rather than from the fixture: the strip is asked
   * for a driver that has really ticked that many times, which is the case the
   * limit is about. `stripLimit` is the rule and `tick-strip.test.ts` pins the
   * cliff; what is read here is the rendered bar count, the one reading that
   * proves the lane was actually drawn apart on screen.
   */
  await check('the epic tick strip keeps its newest end inside its box', async () => {
    /** The batcher's narrowest mark, mirroring `MIN_MARK_W` in `$lib/time/batch`
     *  — the floor this whole reading is about. */
    const MIN_MARK_W = 4;

    for (const width of [390, WIDTH]) {
      const ctx = await browser.newContext({ viewport: { width, height: 844 } });
      const page = await ctx.newPage();
      try {
        // A driver that has ticked as many times as the page asks for, paced
        // evenly across six hours: the best case the data can offer, so a lane
        // that cannot draw THESE apart cannot draw any real driver apart. The
        // count comes off the page's own `limit`, so this follows the limit
        // rather than restating it.
        await page.route('**/api/epics/*/tick-strip*', async (route) => {
          const limit = Number(new URL(route.request().url()).searchParams.get('limit')) || 24;
          const span = 6 * 60 * 60 * 1000;
          const first = Date.now() - span;
          const rows = Array.from({ length: limit }, (_, i) => {
            const started = first + Math.round((i * span) / Math.max(1, limit - 1));
            return {
              id: 9000 + i,
              ts_started: new Date(started).toISOString(),
              ts_finished: new Date(started + 2000).toISOString(),
              outcome: 'nothing',
              signal: 'progressing',
              n_dispatched: 0,
              n_merged: 0,
              n_held: 0,
              n_stuck: 0,
              journal_tick_id: null,
              error_class: null
            };
          });
          await route.fulfill({ json: rows });
        });

        await page.goto(`${base}${EPIC}`, { waitUntil: 'load' });
        // Below 640 the strip is folded behind the legend's disclosure (D16);
        // opened, it is fitted to its box like any other.
        const fold = page.locator('[data-fold-toggle]');
        await page.locator('[data-legend]').first().waitFor({ state: 'visible', timeout: 15_000 });
        if (await fold.isVisible()) await fold.click();
        const strip = page.locator('svg[aria-label*="driver ticks"]');
        await strip.waitFor({ state: 'visible', timeout: 15_000 });
        await sleep(SETTLE_MS);

        const read = await strip.evaluate((svg) => {
          // The clip is on an ANCESTOR, not on the strip: walk out to the first
          // box that does not let its x axis overflow, which is the box the
          // strip has to end inside of.
          let clip = svg.parentElement;
          while (clip && getComputedStyle(clip).overflowX === 'visible') clip = clip.parentElement;
          const box = (clip ?? document.documentElement).getBoundingClientRect();
          const r = svg.getBoundingClientRect();
          // The newest attempt is the rightmost bar, and `MIN_MARK_W` of it is
          // the least a reader can see — so the reading is where the lane ends,
          // which is where that bar is.
          const bars = [...svg.querySelectorAll('rect')].map((el) => {
            const b = el.getBoundingClientRect();
            return { right: b.right, width: b.width };
          });
          return {
            left: r.left,
            right: r.right,
            width: r.width,
            lane: Number(svg.getAttribute('width')),
            clipLeft: box.left,
            clipRight: box.right,
            clipName: (clip ?? document.documentElement).className,
            newestRight: bars.length ? Math.max(...bars.map((b) => b.right)) : null,
            bars: bars.length,
            // What the strip CLAIMS it is showing: "<ref> driver ticks: N
            // attempts". A claim of N with one bar to show for it is the
            // collapse, and this is the only place that pair is readable.
            attempts: Number(/(\d+) attempts/.exec(svg.getAttribute('aria-label') ?? '')?.[1])
          };
        });

        expect(read.width > 0, `the strip inked nothing at ${width}px`);
        expect(read.bars > 0, `the strip drew no bars at ${width}px, so this proves nothing`);
        // Half a pixel: a strip fitted exactly to its box ends ON the edge.
        expect(
          read.right <= read.clipRight + 0.5,
          `the strip is cut off at ${width}px: it ends at ${read.right.toFixed(1)} ` +
            `inside a box ending at ${read.clipRight.toFixed(1)} (${read.clipName})`
        );
        expect(
          read.left >= read.clipLeft - 0.5,
          `the strip starts left of its box at ${width}px: ${read.left.toFixed(1)} ` +
            `against ${read.clipLeft.toFixed(1)}`
        );
        expect(
          read.newestRight <= read.clipRight + 0.5,
          `the newest tick is outside the visible box at ${width}px: its bar ends at ` +
            `${read.newestRight.toFixed(1)} against ${read.clipRight.toFixed(1)}`
        );
        // Every attempt the strip claims is a bar a reader can tell apart from
        // its neighbour — which is what the 4px floor takes away all at once
        // when the lane is given more attempts than it has room for.
        expect(
          read.bars === read.attempts,
          `the strip claims ${read.attempts} attempts at ${width}px but drew ` +
            `${read.bars} bars: the lane cannot draw them apart`
        );
        // The same reading from the other side: the count it drew is one its own
        // lane has the pixels for.
        expect(
          read.attempts <= Math.floor((read.lane - MIN_MARK_W) / MIN_MARK_W),
          `the strip took ${read.attempts} attempts into a ${read.lane}px lane at ` +
            `${width}px, which has room for ` +
            `${Math.floor((read.lane - MIN_MARK_W) / MIN_MARK_W)}`
        );

        // The page itself still must not scroll to reach any of it.
        const pageBox = await page.evaluate(() => ({
          scroll: document.documentElement.scrollWidth,
          client: document.documentElement.clientWidth
        }));
        expect(
          pageBox.scroll <= pageBox.client,
          `the page scrolls sideways at ${width}px: scrollWidth ${pageBox.scroll} ` +
            `over clientWidth ${pageBox.client}`
        );
        // 1440 unchanged: the strip is still the width the design draws.
        if (width === WIDTH)
          expect(
            Math.abs(read.width - 760) <= 0.5,
            `the desktop strip is ${read.width.toFixed(1)}px, not the design's 760px`
          );
      } finally {
        await page.close();
        await ctx.close();
      }
    }
  });
}
