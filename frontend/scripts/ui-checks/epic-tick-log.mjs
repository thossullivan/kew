/**
 * The epic page agrees with its own tick log, a finished epic's timeline fits
 * its own span, and the node popover keeps its action row inside its box
 * (#2783).
 *
 * One surface of `npm run ui-check`; `scripts/ui-check.mjs` runs it (#2570).
 */
// ui-check-owns: src/lib/epic/EpicTimeline.svelte
// ui-check-owns: src/lib/epic/timeline.ts
// ui-check-owns: src/routes/(factory)/epics/[ref]/**
// ui-check-owns: fixtures/factory/epics-launch-fixes*.json
import { EPIC } from '../ui-check-lib.mjs';

/** The two widths the epic page is judged at. */
const PHONE = { width: 390, height: 844 };

/** How far into the lane a fitted timeline's first bar may start, and how far
 *  from its right edge its last bar may end, as a fraction of the lane. */
const EDGE = 0.15;

const DAY_MS = 86_400_000;

/** The fixture node whose popover is tallest. */
const TALLEST = '2055';

/**
 * The fixture timeline, cut down to the nodes that have finished and read
 * seventeen days after the last of them — the complete epic N21 photographed,
 * whose fit ran out to "now" and drew every bar in the left tenth. Built off
 * the served payload rather than written out here, so the bars are the
 * fixture's own runs.
 */
function finishedTimeline(served) {
  const nodes = served.nodes.filter(
    (node) => node.bands.at(-1)?.status === 'done' && node.runs.every((run) => run.finished_at)
  );
  const stamps = nodes.flatMap((node) => [
    ...node.bands.flatMap((band) => [band.from, band.to]),
    ...node.runs.flatMap((run) => [run.started_at, run.finished_at]),
    ...node.rounds.map((round) => round.recorded_at),
    node.merged_at
  ]);
  const last = Math.max(...stamps.filter(Boolean).map((iso) => Date.parse(iso)));
  const now = new Date(last + 17 * DAY_MS).toISOString();
  return { ...served, now, span: { start: served.span.start, end: now }, nodes };
}

export async function checks({ check, visit, expect, sleep, sizes: { WIDTH, HEIGHT } }) {
  /**
   * N18 — the header said "last tick —" (and "no heartbeat yet") above a strip
   * of ticks, because it read the change journal's stamp alone and the
   * fixture's journal has none. The strip, the list and the header now read
   * one tick log, so a strip with ticks in it means a last tick with a time.
   */
  await check('the epic header reads a last tick off the tick strip it draws', async () => {
    for (const size of [{ width: WIDTH, height: HEIGHT }, PHONE]) {
      await visit(
        EPIC,
        async (page) => {
          // Below 640 the strip is folded behind the legend's disclosure (D16).
          const fold = page.locator('[data-fold-toggle]');
          if (await fold.isVisible()) await fold.click();
          const strip = page.locator('svg[aria-label*="driver ticks"]');
          await strip.waitFor({ state: 'visible', timeout: 15_000 });
          const ticks = Number(/(\d+) attempts/.exec((await strip.getAttribute('aria-label')) ?? '')?.[1]);
          expect(ticks > 0, `the strip claims no attempts at ${size.width}px, so this proves nothing`);
          const header = page.locator('section[aria-label^="Epic"] span', { hasText: /^last tick/ });
          await header.first().waitFor({ state: 'visible', timeout: 5_000 });
          const reading = (await header.first().innerText()).replace(/^last tick\s*/, '').trim();
          expect(
            reading !== '—' && reading !== '' && reading !== 'no tick recorded',
            `the header reads "last tick ${reading}" above ${ticks} ticks at ${size.width}px`
          );
          const banner = await page.locator('section[aria-label^="Epic"]').innerText();
          expect(
            !banner.includes('no heartbeat yet'),
            `the header says "no heartbeat yet" above ${ticks} ticks at ${size.width}px`
          );
        },
        size
      );
    }
  });

  /**
   * N21 — a complete epic's timeline opens fitted to its own span: its first
   * bar near the left edge of the lane and its last bar near the right one,
   * not every bar in the left tenth and the rest of the chart empty to "now".
   */
  await check('a complete epic`s timeline fits its own span', async () => {
    // Below 640 the timeline is a sentence, not a plot (D19, epic-fit), so the
    // narrow reading is taken at 768.
    for (const size of [{ width: WIDTH, height: HEIGHT }, { width: 768, height: 1024 }]) {
      await visit(
        `${EPIC}?view=timeline`,
        async (page) => {
          const bars = page.locator('.tl .lane a rect');
          await bars.first().waitFor({ state: 'attached', timeout: 15_000 });
          await sleep(300);
          const read = await page.evaluate(() => {
            const lane = document.querySelector('.tl .ruler')?.getBoundingClientRect();
            const rects = [...document.querySelectorAll('.tl .lane a rect')].map((el) =>
              el.getBoundingClientRect()
            );
            return {
              left: lane?.left ?? 0,
              width: lane?.width ?? 0,
              first: Math.min(...rects.map((r) => r.left)),
              last: Math.max(...rects.map((r) => r.right)),
              count: rects.length
            };
          });
          expect(read.width > 0 && read.count > 0, `no lane or no bars at ${size.width}px`);
          const firstAt = (read.first - read.left) / read.width;
          const lastAt = (read.last - read.left) / read.width;
          expect(
            firstAt >= 0 && firstAt <= EDGE,
            `the first bar starts ${(firstAt * 100).toFixed(1)}% into the lane at ${size.width}px`
          );
          expect(
            lastAt >= 1 - EDGE && lastAt <= 1,
            `the last bar ends ${(lastAt * 100).toFixed(1)}% into the lane at ${size.width}px`
          );
        },
        size,
        async (page) => {
          await page.route('**/api/epics/*/timeline*', async (route) => {
            const served = await (await route.fetch()).json();
            await route.fulfill({ json: finishedTimeline(served) });
          });
        }
      );
    }
  });

  /**
   * N12 — the popover's action row ("Open in Work →", "Issue") was cut off at
   * its bottom edge: the box scrolled as a whole, so a body taller than the
   * cap pushed the row past the fold. The row is the popover's foot now. Read
   * on a short desktop window too, which is where the cap bites.
   */
  await check('the node popover keeps its action row inside its box', async () => {
    for (const size of [{ width: WIDTH, height: HEIGHT }, { width: WIDTH, height: 560 }, PHONE]) {
      await visit(
        EPIC,
        async (page) => {
          // The needs-you node: its popover carries the reason and the command
          // as well as the links, so it is the tallest the fixture can open.
          const node = page.locator(`.dag .svelte-flow__node[data-id="${TALLEST}"]`);
          await node.first().waitFor({ state: 'attached', timeout: 15_000 });
          const dialog = page.locator('[role="dialog"]').filter({ has: page.locator('.links') });
          for (let attempt = 0; attempt < 3 && (await dialog.count()) === 0; attempt += 1) {
            await node.first().click();
            await sleep(300);
          }
          expect((await dialog.count()) > 0, `no popover opened at ${size.width}x${size.height}`);
          await dialog.first().waitFor({ state: 'visible', timeout: 5_000 });
          const read = await dialog.first().evaluate((pop) => {
            const box = pop.getBoundingClientRect();
            const links = pop.querySelector('.links')?.getBoundingClientRect() ?? null;
            return {
              box: { top: box.top, bottom: box.bottom, left: box.left, right: box.right },
              links: links && {
                top: links.top,
                bottom: links.bottom,
                left: links.left,
                right: links.right,
                height: links.height
              },
              viewport: window.innerHeight
            };
          });
          const at = `${size.width}x${size.height}`;
          expect(read.links !== null && read.links.height > 0, `the popover has no action row at ${at}`);
          expect(
            read.links.top >= read.box.top - 0.5 &&
              read.links.bottom <= read.box.bottom + 0.5 &&
              read.links.left >= read.box.left - 0.5 &&
              read.links.right <= read.box.right + 0.5,
            `the action row (${read.links.top.toFixed(1)}–${read.links.bottom.toFixed(1)}) ` +
              `is outside the popover (${read.box.top.toFixed(1)}–${read.box.bottom.toFixed(1)}) at ${at}`
          );
          expect(
            read.links.bottom <= read.viewport + 0.5,
            `the action row ends below the window at ${at}`
          );
        },
        size
      );
    }
  });
}
