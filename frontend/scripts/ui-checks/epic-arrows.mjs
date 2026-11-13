/**
 * The epic graph's ← → keys after a toolbar click and with nothing selected
 * (#2780).
 *
 * One surface of `npm run ui-check`; `scripts/ui-check.mjs` runs it (#2570).
 */
// ui-check-owns: src/routes/(factory)/epics/[ref]/**
// ui-check-owns: fixtures/factory/epics-launch-fixes*.json
import { EPIC } from '../ui-check-lib.mjs';

export async function checks({ check, visit, expect, sleep, sizes: { WIDTH, HEIGHT } }) {
  /**
   * The committed fixture epic has nothing to fold, so its done toggle is the
   * disabled one. The toggle half is served a graph that can fold: four
   * finished nodes in a chain into one in review.
   */
  const CHAIN = ['3001', '3002', '3003', '3004'];
  const IDS = [...CHAIN, '3005'];
  const foldable = (page) =>
    page.route('**/api/epics/launch-fixes', async (route) => {
      const response = await route.fetch();
      const payload = await response.json();
      const [template] = payload.snapshot.nodes;
      const node = (id, status) => ({
        ...template,
        id,
        title: `arrow-keys #${id}`,
        status,
        url: `https://github.com/thossullivan/kew/issues/${id}`,
        attention: null,
        stuck: null,
        motion: 'settled',
        alive: false
      });
      payload.snapshot.nodes = [...CHAIN.map((id) => node(id, 'done')), node('3005', 'in_review')];
      payload.snapshot.edges = IDS.slice(1).map((id, i) => [IDS[i], id]);
      payload.snapshot.frontier = [];
      await route.fulfill({ json: payload });
    });

  /**
   * The fixture epic with nobody waited on. The page opens on the node that
   * needs you when there is one (`openingPick`), so only an epic with none is
   * a fresh load with NOTHING selected.
   */
  const nobodyWaitedOn = (page) =>
    page.route('**/api/epics/launch-fixes', async (route) => {
      const response = await route.fetch();
      const payload = await response.json();
      payload.snapshot.nodes = payload.snapshot.nodes.map((n) => ({ ...n, attention: null }));
      await route.fulfill({ json: payload });
    });

  for (const size of [{ width: WIDTH, height: HEIGHT }, { width: 390, height: 844 }]) {
    const at = `${size.width}x${size.height}`;

    await check(`→ moves the selection after Zoom in and after Fit view at ${at}`, () =>
      visit(
        `${EPIC}?node=2000`,
        async (page) => {
          const node = () => new URL(page.url()).searchParams.get('node');
          await page.locator('.dag .svelte-flow__node[data-id="2000"]').waitFor({
            state: 'attached',
            timeout: 10_000
          });
          expect(node() === '2000', `the page did not open on ?node=2000 (${page.url()})`);

          await page.locator('.dag button[aria-label="Zoom in"]').click();
          await sleep(300);
          await page.keyboard.press('ArrowRight');
          await sleep(250);
          expect(node() === '2001', `→ after Zoom in left the selection on ${node()}, not 2001`);

          await page.locator('.dag button[aria-label="Fit view"]').click();
          await sleep(400);
          await page.keyboard.press('ArrowRight');
          await sleep(250);
          expect(node() === '2040', `→ after Fit view left the selection on ${node()}, not 2040`);

          await page.locator('.dag button[aria-label="Upstream in lineage"]').click();
          await sleep(250);
          const from = node();
          await page.keyboard.press('ArrowRight');
          await sleep(250);
          expect(node() !== from, `→ after the ← button left the selection on ${from}`);
        },
        size
      )
    );

    await check(`→ moves the selection after the done toggle at ${at}`, () =>
      visit(
        `${EPIC}?node=3001`,
        async (page) => {
          const node = () => new URL(page.url()).searchParams.get('node');
          const toggle = page.locator('.dag button.done-toggle');
          await page.locator('.dag .svelte-flow__node[data-id="3001"]').waitFor({
            state: 'attached',
            timeout: 10_000
          });
          expect(!(await toggle.isDisabled()), 'the done toggle is disabled on a chain that folds');
          expect(node() === '3001', `the page did not open on ?node=3001 (${page.url()})`);

          await toggle.click();
          await sleep(600);
          expect((await toggle.getAttribute('aria-pressed')) === 'true', 'show-done did not turn on');
          await page.keyboard.press('ArrowRight');
          await sleep(250);
          expect(node() === '3002', `→ after the done toggle left the selection on ${node()}, not 3002`);
        },
        size,
        foldable
      )
    );

    await check(`→ on a fresh load with nothing selected selects a node at ${at}`, () =>
      visit(
        EPIC,
        async (page) => {
          const node = () => new URL(page.url()).searchParams.get('node');
          await page.locator('.dag .svelte-flow__node').first().waitFor({
            state: 'attached',
            timeout: 10_000
          });
          expect(node() === null, `a fresh load opened with ?node=${node()}`);
          const ringed = await page.locator('.dag .svelte-flow__node.selected').count();
          expect(ringed === 0, `a fresh load opened with ${ringed} node(s) selected`);
          const focused = await page.evaluate(() => document.activeElement === document.body);
          expect(focused, 'a fresh load put the focus somewhere other than the body');

          await page.keyboard.press('ArrowRight');
          await sleep(250);
          const id = node();
          expect(id !== null, '→ with nothing selected selected nothing');
          const drawn = await page.locator(`.dag .svelte-flow__node[data-id="${id}"]`).count();
          expect(drawn > 0, `→ selected ${id}, which the canvas does not draw`);
        },
        size,
        nobodyWaitedOn
      )
    );
  }
}
