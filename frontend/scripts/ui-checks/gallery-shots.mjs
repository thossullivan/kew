/**
 * The gallery's shots of the c4 fixes reach what they photograph (#2790).
 *
 * One surface of `npm run ui-check`; `scripts/ui-check.mjs` runs it (#2570).
 */
// ui-check-owns: scripts/gallery.mjs
// ui-check-owns: src/lib/palette/Palette.svelte
// ui-check-owns: src/lib/work/DispatchLaunchpad.svelte
// ui-check-owns: src/routes/(factory)/config/+page.svelte
// ui-check-owns: src/lib/epic/DagView.svelte
import { SHOTS, step } from '../gallery.mjs';

/** The epic canvas's zoom, read off the viewport transform. */
async function scale(page) {
  const transform = await page.locator('.dag .svelte-flow__viewport').evaluate((el) => el.style.transform);
  const match = /scale\(([-\d.e]+)\)/.exec(transform);
  return match === null ? null : Number(match[1]);
}

export async function checks({ check, visit, expect, sleep, sizes: { WIDTH, HEIGHT } }) {
  /**
   * #2790 — a gallery shot that names a surface (`shows`) runs its own steps
   * here, in a real browser at both widths, and the surface must be on screen
   * after them: the palette open, the launchpad drawer, the /config model
   * listbox, a selected epic node after the toolbar's Zoom in. A selector that
   * drifts would otherwise photograph the closed page every cycle, and the
   * review would read a missing fix as a regression.
   */
  for (const shot of SHOTS.filter((s) => s.shows)) {
    for (const width of [390, WIDTH]) {
      await check(`the gallery shot ${shot.name} opens what it photographs at ${width}px`, () =>
        visit(
          shot.route,
          async (page, problems) => {
            const before = shot.name === 'epic-node-zoom-in' ? await scale(page) : null;
            for (const s of shot.then) await step(page, s);
            await page.locator(shot.shows).first().waitFor({ state: 'visible', timeout: 5_000 });
            const read = await page.evaluate(() => ({
              scroll: document.documentElement.scrollWidth,
              vw: document.documentElement.clientWidth
            }));
            expect(
              read.scroll <= read.vw,
              `${width}px: ${shot.name} scrolls sideways (${read.scroll} > ${read.vw})`
            );
            if (shot.name === 'epic-node-zoom-in') {
              // The click zoomed, and the selection it was made over stands.
              await sleep(400);
              const after = await scale(page);
              expect(
                before !== null && after !== null && after > before,
                `${width}px: Zoom in left the canvas at scale ${after} (was ${before})`
              );
              const node = new URL(page.url()).searchParams.get('node');
              expect(node === '2055', `${width}px: Zoom in dropped ?node=2055 (${page.url()})`);
            }
            // Page errors only, across every shot. `config-model-current.mjs`
            // holds the /config shot to no console error either (#2846).
            const thrown = problems.filter((p) => p.startsWith('page error:'));
            expect(thrown.length === 0, thrown.join('\n'));
          },
          { width, height: width === 390 ? 844 : HEIGHT }
        )
      );
    }
  }
}
