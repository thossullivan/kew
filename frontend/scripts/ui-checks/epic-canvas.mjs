/**
 * The epic canvas: its size, a node click, and the wheel.
 *
 * One surface of `npm run ui-check`; `scripts/ui-check.mjs` runs it (#2570).
 *
 * And the canvas on a phone (D16): at 390 the header put ~420px above the
 * graph, and the canvas showed ~300px of it. Below 640 the legend and the tick
 * strip fold behind one disclosure, the needs-you count stays outside it, and
 * the canvas keeps DESIGN.md's 400px floor.
 */
// ui-check-owns: src/lib/epic/DagView.svelte
// ui-check-owns: src/routes/(factory)/epics/[ref]/**
// ui-check-owns: fixtures/factory/epics-launch-fixes*.json
import { EPIC } from '../ui-check-lib.mjs';

export async function checks({ check, visit, expect, sleep }) {
  await check('epic canvas has a size and nodes', () =>
    visit(EPIC, async (page) => {
      // `attached`, not `visible`: a zero-height canvas is "hidden" to
      // Playwright, and the reading this wants is the SIZE, reported.
      const dag = page.locator('.dag');
      await dag.waitFor({ state: 'attached', timeout: 15_000 });
      const box = await dag.boundingBox();
      expect(box !== null, '.dag is not laid out at all');
      expect(box.width > 0 && box.height > 0, `.dag rendered at ${box.width}x${box.height}`);
      await page
        .locator('.dag .svelte-flow__node')
        .first()
        .waitFor({ state: 'attached', timeout: 10_000 })
        .catch(() => {});
      const nodes = await page.locator('.dag .svelte-flow__node').count();
      expect(nodes > 0, 'the canvas drew no .svelte-flow__node');
    })
  );

  await check('a node click opens the popover and writes ?node=', () =>
    visit(EPIC, async (page) => {
      const dialog = page.locator('.dag [role="dialog"]');
      // A click on a COLLAPSED CHAIN expands it instead of selecting, so take
      // the first node that answers with a popover rather than node zero.
      for (let attempt = 0; attempt < 4; attempt += 1) {
        const nodes = page.locator('.dag .svelte-flow__node');
        const count = await nodes.count();
        expect(count > 0, 'the canvas drew no .svelte-flow__node');
        await nodes.nth(Math.min(attempt, count - 1)).click();
        await sleep(250);
        if (await dialog.count()) break;
      }
      expect(await dialog.count() > 0, 'no [role="dialog"] popover opened on a node click');
      await dialog.first().waitFor({ state: 'visible', timeout: 5_000 });
      const node = new URL(page.url()).searchParams.get('node');
      expect(node !== null && node !== '', `the URL kept no ?node= (${page.url()})`);
    })
  );

  await check('the wheel over the canvas zooms the viewport', () =>
    visit(EPIC, async (page) => {
      const flow = page.locator('.dag .svelte-flow__viewport');
      await flow.waitFor({ state: 'attached', timeout: 10_000 });
      const scale = async () => {
        const transform = await flow.evaluate((el) => el.style.transform);
        const match = /scale\(([-\d.e]+)\)/.exec(transform);
        expect(match !== null, `no scale() in the viewport transform (${transform})`);
        return Number(match[1]);
      };
      const before = await scale();
      const box = await page.locator('.dag').boundingBox();
      await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
      await page.mouse.wheel(0, -400);
      await sleep(400);
      const after = await scale();
      expect(after !== before, `the wheel left the scale at ${before}`);
    })
  );

  await check('at 390x844 the canvas is 400px tall under a folded legend and tick strip', () =>
    visit(
      EPIC,
      async (page) => {
        await page.locator('.dag').first().waitFor({ state: 'attached', timeout: 15_000 });
        const read = await page.evaluate(() => {
          const height = (el) => (el ? el.getBoundingClientRect().height : 0);
          const shown = (el) => {
            if (!el) return false;
            const r = el.getBoundingClientRect();
            return r.width > 0 && r.height > 0 && getComputedStyle(el).visibility !== 'hidden';
          };
          const toggle = document.querySelector('[data-fold-toggle]');
          const legend = document.querySelector('[data-legend]');
          const words = [...(legend?.querySelectorAll('[data-status-word]') ?? [])];
          return {
            pane: height(document.querySelector('[data-canvas-pane]')),
            dag: height(document.querySelector('.dag')),
            toggle: shown(toggle) ? { expanded: toggle.getAttribute('aria-expanded'), h: height(toggle) } : null,
            statusWords: words.length,
            wordsShown: words.filter(shown).length,
            stripShown: [...document.querySelectorAll('[data-fold]')].some(
              (el) => !el.closest('[data-legend]') && shown(el)
            ),
            needsYou: shown(document.querySelector('[data-epic-needs-you]'))
          };
        });
        expect(read.pane >= 400, `the canvas pane is ${Math.round(read.pane)}px tall`);
        expect(read.dag >= 400, `the canvas is ${Math.round(read.dag)}px tall`);
        expect(read.toggle !== null, 'no disclosure folds the legend and the tick strip at 390');
        expect(read.toggle.expanded === 'false', `the disclosure opens ${read.toggle.expanded}`);
        expect(read.toggle.h >= 32, `the disclosure is ${read.toggle.h}px tall`);
        expect(read.statusWords > 0, 'the legend has no status words to fold');
        expect(read.wordsShown === 0, `${read.wordsShown} legend words are drawn under a shut disclosure`);
        expect(!read.stripShown, 'the tick strip is drawn under a shut disclosure');
        expect(read.needsYou, 'the needs-you count folded away with the legend');
        // And it opens.
        await page.locator('[data-fold-toggle]').click();
        await sleep(300);
        const open = await page.evaluate(() => {
          const shown = (el) => {
            const r = el.getBoundingClientRect();
            return r.width > 0 && r.height > 0;
          };
          return {
            words: [...document.querySelectorAll('[data-legend] [data-status-word]')].filter(shown).length,
            strip: [...document.querySelectorAll('[data-fold]')].some((el) => !el.closest('[data-legend]') && shown(el)),
            scroll: document.documentElement.scrollWidth - document.documentElement.clientWidth
          };
        });
        expect(open.words > 0, 'the open disclosure shows no legend');
        expect(open.strip, 'the open disclosure shows no tick strip');
        expect(open.scroll <= 0, `the open legend scrolls the page sideways by ${open.scroll}px`);
        // The disclosure names what it folds, and every name is on the page.
        const refs = await page.evaluate(() => {
          const ids = (document.querySelector('[data-fold-toggle]')?.getAttribute('aria-controls') ?? '')
            .split(/\s+/)
            .filter(Boolean);
          return { ids, missing: ids.filter((id) => document.getElementById(id) === null) };
        });
        expect(refs.ids.length > 0, 'the disclosure names nothing it controls');
        expect(refs.missing.length === 0, `the disclosure controls ids nothing has: ${refs.missing.join(', ')}`);
      },
      { width: 390, height: 844 }
    )
  );

  // Design review c9: at 1366x650 the graph area was ~307px tall, `.dag`'s
  // 360px floor holding its control bar too. In a laptop window 600-799px tall
  // and on a phone the node area itself keeps 360px; the page scrolls the
  // difference, the panes below move down, and nothing overlaps the canvas.
  // A taller desktop window takes what is left (from 900px the page may not
  // scroll), its control bar still whole. Negative control: with the rule in
  // DagView removed the 1366x650 reading is ~307px and the 390x844 one ~305px.
  for (const [width, height, least] of [
    [1440, 960, 300],
    [1366, 650, 360],
    [390, 844, 360]
  ]) {
    await check(`the graph area is at least ${least}px tall with nothing over it at ${width}x${height}`, () =>
      visit(
        `${EPIC}?node=2055`,
        async (page) => {
          await page.locator('.dag .floor').first().waitFor({ state: 'attached', timeout: 15_000 });
          await sleep(300);
          const read = await page.evaluate(() => {
            const box = (el) => (el ? el.getBoundingClientRect() : null);
            const floor = box(document.querySelector('.dag .floor'));
            const dag = box(document.querySelector('.dag'));
            const pane = box(document.querySelector('[data-canvas-pane]'));
            const bar = box(document.querySelector('.dag > .bar'));
            const strip = box(document.querySelector('.strip'));
            const rail = box(document.querySelector('section[aria-label^="Selected node"]'));
            return {
              floor: floor?.height ?? 0,
              dagBottom: dag?.bottom ?? 0,
              paneBottom: pane?.bottom ?? 0,
              railTop: rail?.top ?? null,
              // The control bar is whole inside the canvas, and the strip
              // under it starts below it.
              barCut: bar !== null && dag !== null && (bar.height === 0 || bar.bottom > dag.bottom + 1),
              stripOver: bar !== null && strip !== null && strip.top < bar.bottom - 1,
              sideways: document.documentElement.scrollWidth > document.documentElement.clientWidth + 1
            };
          });
          expect(read.floor >= least, `the graph area is ${Math.round(read.floor)}px tall`);
          expect(read.dagBottom <= read.paneBottom + 1, 'the canvas runs past its own pane');
          expect(!read.barCut, "the canvas's control bar is cut off at its foot");
          expect(!read.stripOver, "the No dependencies strip starts over the canvas's control bar");
          expect(
            read.railTop === null || read.railTop >= read.paneBottom - 1,
            `the rail starts at ${read.railTop}, over the canvas pane ending at ${read.paneBottom}`
          );
          expect(!read.sideways, 'the epic page scrolls sideways');
          // What is below the canvas is reached by scrolling, never cut off.
          if (read.railTop !== null) {
            await page.locator('section[aria-label^="Selected node"]').first().scrollIntoViewIfNeeded();
            const seen = await page
              .locator('section[aria-label^="Selected node"]')
              .first()
              .evaluate((el) => {
                const r = el.getBoundingClientRect();
                return r.top < window.innerHeight && r.bottom > 0;
              });
            expect(seen, 'the rail below the canvas cannot be scrolled into view');
          }
        },
        { width, height }
      )
    );
  }
}
