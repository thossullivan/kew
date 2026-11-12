/**
 * The epic graph's #2543 review findings, at both of the issue's windows.
 *
 * One surface of `npm run ui-check`; `scripts/ui-check.mjs` runs it (#2570).
 */
// ui-check-owns: src/routes/(factory)/epics/[ref]/**
// ui-check-owns: fixtures/factory/epics-go-live-2026-09*.json
// ui-check-owns: fixtures/factory/epics-launch-fixes*.json
// ui-check-owns: fixtures/factory/epics-sweep-2026-10*.json
import { EPIC } from '../ui-check-lib.mjs';

/** The fixture epic with no edge between any two nodes (#2543). */
const EDGELESS = '/epics/sweep-2026-10';
/** The Linear epic the first-frame zoom was read off (#2543, D28). */
const GO_LIVE_ROUTE = '/epics/go-live-2026-09';

/** The two windows #2543's gates name: the host-contract desktop and a phone. */
const EPIC_WINDOWS = [
  { width: 1440, height: 900 },
  { width: 390, height: 844 }
];

/** Whether two bounding boxes overlap by any area. Touching is not overlap. */
function intersects(a, b) {
  return a.x < b.x + b.width && b.x < a.x + a.width && a.y < b.y + b.height && b.y < a.y + a.height;
}

export async function checks({ check, visit, expect, sleep }) {
  /**
   * #2543 — the epic graph's four review findings, at both of the issue's
   * windows. D27: an epic with no edges drew an empty canvas with zoom
   * controls over a text strip; it is a card grid now. D28: go-live's first
   * frame fitted everything at 20%. D29: the "No dependencies" strip was cut
   * in half by the rail at 1440x900. D30: card status rows were cut mid-word.
   */
  for (const size of EPIC_WINDOWS) {
    const at = `${size.width}x${size.height}`;

    await check(`an edge-less epic is one card per node, with no canvas, at ${at}`, () =>
      visit(
        EDGELESS,
        async (page) => {
          const ref = EDGELESS.split('/').pop();
          const detail = await page.evaluate(
            (r) => fetch(`/api/epics/${r}`).then((res) => res.json()),
            ref
          );
          const want = detail.snapshot.nodes.length;
          expect(detail.snapshot.edges.length === 0, `the ${ref} fixture has edges`);
          const cards = page.locator('.card-grid .grid-card');
          await cards.first().waitFor({ state: 'visible', timeout: 15_000 });
          const flow = await page.locator('.svelte-flow__node').count();
          expect(flow === 0, `an edge-less epic drew ${flow} .svelte-flow__node`);
          expect((await page.locator('.dag').count()) === 0, 'an edge-less epic drew the canvas');
          expect((await page.locator('.strip').count()) === 0, 'an edge-less epic drew the strip');
          const drawn = await cards.count();
          expect(drawn === want, `the grid drew ${drawn} cards for ${want} nodes`);
          const ids = await page.locator('.card-grid .grid-card .card .id').allTextContents();
          expect(new Set(ids).size === want, `the grid's ${drawn} cards name ${new Set(ids).size} nodes`);
          const head = (await page.locator('.cards').getAttribute('aria-label')) ?? '';
          expect(head === `${want} independent nodes`, `the grid is labelled "${head}"`);
          // A card is a control: it selects the node, and the rail follows.
          await cards.nth(0).click();
          const rail = page.locator('section[aria-label^="Selected node #"]');
          await rail.waitFor({ state: 'attached', timeout: 5_000 });
          expect(new URL(page.url()).searchParams.get('node') !== null, 'a grid card click wrote no ?node=');
        },
        size
      )
    );

    await check(`the go-live first frame reads (zoom >= 0.6) and Fit all shows everything at ${at}`, () =>
      visit(
        GO_LIVE_ROUTE,
        async (page) => {
          await page.locator('.svelte-flow__node').first().waitFor({ state: 'attached', timeout: 15_000 });
          const zoomOf = () =>
            page.evaluate(() => {
              const t = document.querySelector('.svelte-flow__viewport')?.style.transform ?? '';
              const m = /scale\(([\d.]+)\)/.exec(t);
              return m ? Number(m[1]) : null;
            });
          const zoom = await zoomOf();
          expect(zoom !== null, 'no .svelte-flow__viewport transform to read a zoom off');
          expect(zoom >= 0.6, `the first frame opened at zoom ${zoom}`);
          // Fit all (F) still shows every card, whatever zoom that takes.
          await page.locator('.dag').focus();
          await page.keyboard.press('f');
          await sleep(600);
          const floor = await page.locator('.floor').boundingBox();
          const boxes = await page.locator('.svelte-flow__node').evaluateAll((els) =>
            els.map((el) => {
              const r = el.getBoundingClientRect();
              return { id: el.dataset.id, x: r.x, y: r.y, width: r.width, height: r.height };
            })
          );
          const out = boxes.filter(
            (b) =>
              b.x < floor.x - 1 ||
              b.y < floor.y - 1 ||
              b.x + b.width > floor.x + floor.width + 1 ||
              b.y + b.height > floor.y + floor.height + 1
          );
          expect(out.length === 0, `Fit all left ${out.map((b) => b.id).join(', ')} off the canvas`);
        },
        size
      )
    );

    for (const route of [EPIC, GO_LIVE_ROUTE]) {
      await check(`the "No dependencies" strip clears the rail on ${route} at ${at}`, () =>
        visit(
          route,
          async (page) => {
            const strip = page.locator('.strip');
            await strip.waitFor({ state: 'attached', timeout: 15_000 });
            const rail = page.locator('section[aria-label^="Selected node"]');
            await rail.waitFor({ state: 'attached', timeout: 15_000 });
            const s = await strip.boundingBox();
            const r = await rail.boundingBox();
            expect(s !== null && r !== null, 'the strip or the rail is not laid out');
            expect(s.height > 0, 'the strip has no height');
            expect(
              !intersects(s, r),
              `the strip (y ${Math.round(s.y)}-${Math.round(s.y + s.height)}) runs under the rail (y ${Math.round(r.y)})`
            );
          },
          size
        )
      );

      await check(`no card status word is cut mid-word on ${route} at ${at}`, () =>
        visit(
          route,
          async (page) => {
            await page.locator('.svelte-flow__node').first().waitFor({ state: 'attached', timeout: 15_000 });
            // Every NODE card (a collapsed chain's card has no status words).
            // Clipped is measured, not inferred from markup: any box in the
            // row whose content is wider than the box is cutting text — an
            // ellipsis, or a word under the badge. A word that WRAPPED out of
            // the row's one line is whole, and is not counted.
            const cut = await page.locator('.svelte-flow__node .card .sl').evaluateAll((rows) =>
              rows
                .filter((row) => !row.classList.contains('split'))
                .flatMap((row) => {
                  const id = row.closest('.card')?.querySelector('.id')?.textContent ?? '?';
                  const bad = [];
                  for (const el of [row, ...row.querySelectorAll('*')]) {
                    if (getComputedStyle(el).display === 'inline') continue;
                    if (el.scrollWidth > el.clientWidth + 1)
                      bad.push(`${id}: "${el.textContent}" is clipped (${el.scrollWidth} in ${el.clientWidth}px)`);
                  }
                  return bad;
                })
            );
            expect(cut.length === 0, cut.join('\n'));
          },
          size
        )
      );
    }
  }
}
