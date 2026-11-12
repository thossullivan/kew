/**
 * The epic done toggle and lineage arrows (#2533 / #2560).
 *
 * One surface of `npm run ui-check`; `scripts/ui-check.mjs` runs it (#2570).
 */
// ui-check-owns: src/routes/(factory)/epics/[ref]/**
// ui-check-owns: fixtures/factory/epics-launch-fixes*.json
import { EPIC, SETTLE_MS } from '../ui-check-lib.mjs';

export async function checks({ check, visit, expect, sleep, sizes: { WIDTH, HEIGHT } }) {
  /**
   * #2533 / #2560 — the done toggle and the lineage arrows, driven by a pointer
   * at the two widths Tom's review read them at.
   *
   * The committed fixture epic has nothing to fold: every finished node in it
   * (2001, 2021, 2047, 1969) feeds open work directly, and a collapse keeps
   * those, which leaves no finished chain of two behind them. So the toggle
   * there is the disabled one that says why.
   *
   * The folding half is served HOST_CONTRACT instead: the live epic the toggle
   * did nothing on, eight finished nodes in a chain into #2399, the finished
   * node #2526 (in review, needs you) depends on. It folds to one "8 done"
   * card beside #2399.
   */
  const LAUNCH_EDGES = [
    ['2001', '2040'], ['2021', '2040'], ['1969', '2054'], ['1969', '2058'], ['1969', '2060'],
    ['2047', '2053'], ['2000', '2001'], ['2021', '2047'], ['2047', '2055']
  ];
  const HC_CHAIN = ['2378', '2379', '2380', '2381', '2397', '2398', '2411', '2433'];
  const HC_EDGES = [
    ...HC_CHAIN.slice(1).map((id, i) => [HC_CHAIN[i], id]),
    ['2433', '2399'],
    ['2399', '2526']
  ];
  /** Serve the epic page HOST_CONTRACT's graph in place of the fixture's. */
  const hostContract = (page) =>
    page.route('**/api/epics/launch-fixes', async (route) => {
      const response = await route.fetch();
      const payload = await response.json();
      const [template] = payload.snapshot.nodes;
      const node = (id, status, extra = {}) => ({
        ...template,
        id,
        title: `host-contract #${id}`,
        status,
        url: `https://github.com/thossullivan/kew/issues/${id}`,
        attention: null,
        stuck: null,
        motion: 'settled',
        alive: false,
        ...extra
      });
      payload.snapshot.nodes = [
        ...HC_CHAIN.map((id) => node(id, 'done')),
        node('2399', 'done'),
        node('2526', 'in_review', {
          attention: {
            kind: 'merge_decision',
            reason: 'merge decision on PR #2557',
            url: null,
            action: 'review and merge the PR',
            command: ''
          },
          motion: 'waiting_human',
          alive: true
        })
      ];
      payload.snapshot.edges = HC_EDGES;
      payload.snapshot.frontier = [];
      await route.fulfill({ json: payload });
    });

  for (const size of [{ width: WIDTH, height: HEIGHT }, { width: 390, height: 844 }]) {
    const at = `${size.width}x${size.height}`;
    await check(`the done toggle is disabled and says why when nothing can fold at ${at}`, () =>
      visit(
        EPIC,
        async (page) => {
          const toggle = page.locator('.dag button.done-toggle');
          await toggle.waitFor({ state: 'visible', timeout: 10_000 });
          expect(await toggle.isDisabled(), 'the toggle is live on an epic with nothing to fold');
          const title = await toggle.getAttribute('title');
          expect(title === 'No finished chain can fold here', `the disabled toggle says "${title}"`);
          const text = (await toggle.textContent())?.trim();
          // DESIGN.md's `dag-canvas`: disabled, it says why (D39), never "Done · shown".
          expect(text === 'No finished chain can fold here', `the disabled toggle reads "${text}"`);
          const groups = await page.locator('.dag .svelte-flow__node[data-id^="done:"]').count();
          expect(groups === 0, `${groups} group cards drawn on an epic with nothing to fold`);
        },
        size
      )
    );

    await check(`"Done · collapsed" folds host-contract to "8 done" and keeps it on screen at ${at}`, () =>
      visit(
        EPIC,
        async (page) => {
          const cards = page.locator('.dag .svelte-flow__node');
          const card = (id) => page.locator(`.dag .svelte-flow__node[data-id="${id}"]`);
          const drawn = async (id) => (await card(id).count()) > 0;
          const groupCard = page.locator('.dag .svelte-flow__node[data-id^="done:"]');
          const toggle = page.locator('.dag button.done-toggle');
          await groupCard.waitFor({ state: 'attached', timeout: 10_000 });
          const read = async () => (await toggle.textContent())?.trim();

          expect(!(await toggle.isDisabled()), 'the toggle is disabled with a chain to fold');
          expect((await read()) === 'Done · collapsed (8)', `the toggle reads "${await read()}"`);
          expect((await cards.count()) === 3, `collapsed drew ${await cards.count()} cards, not 3`);
          expect(await drawn('2399') && await drawn('2526'), 'the collapse took #2399 or #2526');
          expect((await groupCard.textContent())?.includes('8 done'), 'the group card is not "8 done"');

          // Open the chain by hand first: the toggle must still fold it.
          await page.locator('.dag button[aria-label="Fit view"]').click();
          await sleep(400);
          await groupCard.click();
          await sleep(250);
          expect((await cards.count()) === 10, 'a click on "8 done" did not open it');

          await toggle.click();
          await sleep(400);
          expect((await toggle.getAttribute('aria-pressed')) === 'true', 'show-done did not turn on');
          expect((await read()) === 'Done · shown', `the toggle reads "${await read()}"`);
          expect((await cards.count()) === 10, `show-done drew ${await cards.count()} cards, not 10`);

          await toggle.click();
          // The refit is a 260ms camera move.
          await sleep(600);
          expect((await toggle.getAttribute('aria-pressed')) === 'false', 'show-done did not turn off');
          expect((await cards.count()) === 3, `the collapse drew ${await cards.count()} cards, not 3`);
          const pane = await page.locator('.dag .svelte-flow').first().boundingBox();
          const box = await groupCard.boundingBox();
          expect(pane !== null && box !== null, 'the canvas or the group card is not laid out');
          const inside =
            box.x >= pane.x - 1 &&
            box.y >= pane.y - 1 &&
            box.x + box.width <= pane.x + pane.width + 1 &&
            box.y + box.height <= pane.y + pane.height + 1;
          expect(
            inside,
            `the group card (${JSON.stringify(box)}) is outside the canvas (${JSON.stringify(pane)})`
          );
        },
        size,
        hostContract
      )
    );

    await check(`a ?node= link into a folded chain opens it and walks with ← and → at ${at}`, () =>
      visit(
        `${EPIC}?node=2380`,
        async (page) => {
          const up = page.locator('.dag button[aria-label="Upstream in lineage"]');
          const down = page.locator('.dag button[aria-label="Downstream in lineage"]');
          const node = () => new URL(page.url()).searchParams.get('node');
          const drawn = async (id) =>
            (await page.locator(`.dag .svelte-flow__node[data-id="${id}"]`).count()) > 0;
          await page.locator('.dag .svelte-flow__node[data-id="2380"]').waitFor({
            state: 'attached',
            timeout: 10_000
          });
          expect(node() === '2380', `the page did not open on ?node=2380 (${page.url()})`);
          await up.click();
          await sleep(250);
          expect(node() === '2379', `← from 2380 landed on ${node()}`);
          expect(await drawn('2379'), '← landed on a node the canvas does not draw');
          const walked = [node()];
          for (let step = 0; step < 12 && !(await down.isDisabled()); step += 1) {
            await down.click();
            await sleep(250);
            walked.push(node());
            expect(await drawn(node()), `→ landed on ${node()}, which the canvas does not draw`);
          }
          const want = [...HC_CHAIN.slice(1), '2399', '2526'].join(' → ');
          expect(walked.join(' → ') === want, `→ walked ${walked.join(' → ')}`);
          // Off the chain again, it folds back to its card; ← re-enters it.
          const groups = await page.locator('.dag .svelte-flow__node[data-id^="done:"]').count();
          expect(groups === 1, 'the chain did not fold back once the selection left it');
          await up.click();
          await sleep(250);
          await up.click();
          await sleep(250);
          expect(node() === '2433', `← from 2399 landed on ${node()}, not the chain's last member`);
          expect(await drawn('2433'), '← into the folded chain did not open it');
        },
        size,
        hostContract
      )
    );

    await check(`the lineage arrows walk one edge at a time and stop at the ends at ${at}`, () =>
      visit(
        `${EPIC}?node=2000`,
        async (page) => {
          const up = page.locator('.dag button[aria-label="Upstream in lineage"]');
          const down = page.locator('.dag button[aria-label="Downstream in lineage"]');
          const node = () => new URL(page.url()).searchParams.get('node');
          const parents = (id) => LAUNCH_EDGES.filter(([, d]) => d === id).map(([s]) => s);
          const kids = (id) => LAUNCH_EDGES.filter(([s]) => s === id).map(([, d]) => d);

          // 2000 is a root: ← is closed, → is open.
          expect(node() === '2000', `the page did not open on ?node=2000 (${page.url()})`);
          expect(await up.isDisabled(), '← is live on a root');
          expect(!(await down.isDisabled()), '→ is closed on a node with a child');

          // → down the lineage to its sink, one edge per press.
          const walked = ['2000'];
          for (let step = 0; step < 6 && !(await down.isDisabled()); step += 1) {
            const from = node();
            await down.click();
            await sleep(250);
            expect(kids(from).includes(node()), `→ from ${from} landed on ${node()}, not a child`);
            walked.push(node());
          }
          expect(walked.join(' → ') === '2000 → 2001 → 2040', `→ walked ${walked.join(' → ')}`);
          expect(await down.isDisabled(), '→ is live on a sink');

          // ← back up, one edge per press, to a root.
          for (let step = 0; step < 6 && !(await up.isDisabled()); step += 1) {
            const from = node();
            await up.click();
            await sleep(250);
            expect(parents(from).includes(node()), `← from ${from} landed on ${node()}, not a parent`);
          }
          expect(parents(node()).length === 0, `← stopped on ${node()}, which has a parent`);
          expect(await up.isDisabled(), '← is live on a root');
        },
        size
      )
    );

    /*
     * #2544 — the lineage selection box. `+#2397` is Dagster's `+asset`: the
     * node and one layer of parents keep full weight, every other card dims —
     * so on host-contract's chain everything that is not upstream of #2397
     * (2398 on to 2526) dims, as do the grandparents a single `+` does not
     * reach. The box writes `?select=`, and a reload reads it back.
     *
     * Below 1024 the field is a key hint and is not drawn (#3053 N6), so at
     * 390 the selection arrives as a `?select=` link instead, and it dims the
     * same nodes and survives the same reload.
     */
    await check(`+#2397 dims every node not upstream of #2397, and a reload restores it at ${at}`, () =>
      visit(
        EPIC,
        async (page) => {
          const box = page.locator('#dag-lineage');
          const dimmed = async (id) =>
            (await page.locator(`.dag .svelte-flow__node[data-id="${id}"] .card.dim`).count()) > 0;
          const drawn = async (id) =>
            (await page.locator(`.dag .svelte-flow__node[data-id="${id}"]`).count()) > 0;
          const lit = ['2397', '2381'];
          const downstream = ['2398', '2411', '2433', '2399', '2526'];
          const beyond = ['2378', '2379', '2380'];
          const assertSelection = async (when) => {
            for (const id of [...lit, ...downstream, ...beyond]) {
              expect(await drawn(id), `${when}: #${id} is not drawn (the chain stayed folded)`);
            }
            for (const id of lit) expect(!(await dimmed(id)), `${when}: #${id} is dimmed`);
            for (const id of [...downstream, ...beyond]) {
              expect(await dimmed(id), `${when}: #${id}, not one layer upstream of #2397, is lit`);
            }
          };

          if (size.width < 1024) {
            await page.goto(`${new URL(page.url()).origin}${EPIC}?select=${encodeURIComponent('+#2397')}`, {
              waitUntil: 'load'
            });
            await page.locator('.dag .svelte-flow__node[data-id="2397"]').waitFor({
              state: 'attached',
              timeout: 10_000
            });
            await sleep(SETTLE_MS);
            expect(!(await box.isVisible()), `the lineage field is drawn at ${at}`);
            await assertSelection('linked');
          } else {
            await box.waitFor({ state: 'visible', timeout: 10_000 });
            await box.fill('+#2397');
            await box.press('Enter');
            await sleep(400);
            const select = new URL(page.url()).searchParams.get('select');
            expect(select === '+#2397', `the box wrote ?select=${select} (${page.url()})`);
            await assertSelection('typed');
          }

          await page.reload({ waitUntil: 'load' });
          await page.locator('.dag .svelte-flow__node[data-id="2397"]').waitFor({
            state: 'attached',
            timeout: 10_000
          });
          await sleep(SETTLE_MS);
          expect((await box.inputValue()) === '+#2397', `the box reads "${await box.inputValue()}" after a reload`);
          await assertSelection('reloaded');
          if (size.width < 1024) return;

          // A bad selection says why, in a sentence, and keeps the old one.
          await box.fill('#2397');
          await box.press('Enter');
          await sleep(200);
          const note = (await page.locator('#dag-lineage-note').textContent())?.trim() ?? '';
          expect(note.endsWith('.') && note.includes('#2397'), `the refusal reads "${note}"`);
          expect(new URL(page.url()).searchParams.get('select') === '+#2397', 'a refusal rewrote ?select=');
        },
        size,
        hostContract
      )
    );

    /*
     * Below 1024 the field L fills is not drawn (#3053 N6): L still selects,
     * read off the field's value and `?select=`, and the selection the box
     * would take arrives as a `?select=` link.
     */
    await check(`L fills +#N+ and the arrows walk within the selection at ${at}`, () =>
      visit(
        `${EPIC}?node=2047`,
        async (page) => {
          const box = page.locator('#dag-lineage');
          const up = page.locator('.dag button[aria-label="Upstream in lineage"]');
          const down = page.locator('.dag button[aria-label="Downstream in lineage"]');
          const node = () => new URL(page.url()).searchParams.get('node');
          const wide = size.width >= 1024;
          if (wide) await box.waitFor({ state: 'visible', timeout: 10_000 });
          else expect(!(await box.isVisible()), `the lineage field is drawn at ${at}`);
          await page.locator('.dag .svelte-flow__node[data-id="2047"]').click();
          await sleep(250);
          await page.keyboard.press('l');
          await sleep(400);
          expect((await box.inputValue()) === '+#2047+', `L filled "${await box.inputValue()}"`);
          expect(
            new URL(page.url()).searchParams.get('select') === '+#2047+',
            `L wrote ${page.url()}`
          );
          // `#2021+` is 2021 and its children 2040 and 2047. Unselected, →
          // from 2047 goes on to 2053; inside the selection it is closed.
          if (wide) {
            await box.fill('#2021+');
            await box.press('Enter');
          } else {
            const url = new URL(page.url());
            url.searchParams.set('select', '#2021+');
            await page.goto(url.href, { waitUntil: 'load' });
            await page.locator('.dag .svelte-flow__node[data-id="2047"]').waitFor({
              state: 'attached',
              timeout: 10_000
            });
            await sleep(SETTLE_MS);
          }
          await sleep(400);
          expect(node() === '2047', `the selection moved the node to ${node()}`);
          expect(await down.isDisabled(), '→ from 2047 is live, so it walks out of #2021+');
          await up.click();
          await sleep(250);
          expect(node() === '2021', `← from 2047 landed on ${node()}`);
          await down.click();
          await sleep(250);
          expect(['2040', '2047'].includes(node()), `→ from 2021 left the selection for ${node()}`);
        },
        size
      )
    );
  }
}
