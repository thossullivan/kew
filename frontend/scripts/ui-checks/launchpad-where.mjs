/**
 * "Plan a dispatch" only where dispatch is the next move, with a quiet Run
 * (#2785) — quiet everywhere since #3467 (D29): with nothing needing a
 * person, Work shows no magenta, on the closed line or in the drawer.
 *
 * One surface of `npm run ui-check`; `scripts/ui-check.mjs` runs it (#2570).
 *
 * Design review c5 found the launchpad mounted on every selected item — the
 * in-review #2055, the held SFD-229, and a `stalled_epic:*` row whose key the
 * panel resolved a plan for as if it were an item — with a magenta Run under a
 * needs-you block that already had one, and a closed line printing the raw
 * model id. Each check reads what the BROWSER drew, at both widths.
 */
// ui-check-owns: src/routes/(factory)/work/+page.*
// ui-check-owns: src/lib/work/DispatchLaunchpad.svelte
import { readFileSync } from 'node:fs';
import { FRONTEND, SETTLE_MS } from '../ui-check-lib.mjs';

const served = JSON.parse(
  readFileSync(new URL('fixtures/factory/dashboard.json', FRONTEND), 'utf8')
);
const GO_LIVE = 'go-live-2026-09';
const STALLED = `stalled_epic:${GO_LIVE}`;
/** The fixture ready queue's first item: dispatch is its next move. */
const QUEUED = '2060';

/** The committed set plus a stalled epic, which names no item. */
function withStalledEpic(payload) {
  const stalled = {
    ...payload.rows[0],
    type: 'stalled_epic',
    consequence: 'epic',
    reason: 'the epic driver stalled',
    sentence: 'the epic driver stalled',
    issue_num: null,
    item_id: '',
    epic_ref: GO_LIVE,
    pr_number: null,
    url: null,
    attention_kind: '',
    label: '',
    command: ''
  };
  return { ...payload, rows: [...payload.rows, stalled] };
}

/** The committed set plus a needs-you wait naming the queued item. */
function withQueuedWait(payload) {
  const base = payload.rows.find((r) => r.command) ?? payload.rows[0];
  const wait = { ...base, issue_num: Number(QUEUED), item_id: QUEUED, pr_number: null };
  return { ...payload, rows: [...payload.rows, wait] };
}

/** Serve `/api/dashboard` (exactly that path) through *shape*. */
async function routeDashboard(page, shape) {
  await page.route(
    (url) => url.pathname === '/api/dashboard',
    async (route) => {
      const response = await route.fetch();
      await route.fulfill({ response, json: shape(await response.json()) });
    }
  );
}

/** Every `/api/dispatch/resolve` request the page sends, as it sends it. */
function resolveReads(page) {
  const reads = [];
  page.on('request', (request) => {
    const url = new URL(request.url());
    if (url.pathname === '/api/dispatch/resolve') reads.push(url.searchParams.get('item'));
  });
  return reads;
}

/** Open `/work?item=` and wait until the item's detail pane has drawn. */
async function openItem(page, base, item, sleep) {
  await page.goto(`${base}/work?item=${encodeURIComponent(item)}`, { waitUntil: 'load' });
  await page
    .locator('section[aria-label="Item detail"] h1, section[aria-label="Item detail"] h2')
    .first()
    .waitFor({ state: 'visible', timeout: 15_000 });
  await sleep(SETTLE_MS);
}

const launchpads = (page) => page.locator('section[aria-label="Plan a dispatch"]').count();

/** Visible buttons in the detail pane painted the accent (magenta) fill. */
function magentaButtons(page) {
  return page.evaluate(() => {
    const probe = document.createElement('button');
    probe.className = 'bg-accent';
    document.body.append(probe);
    const accent = getComputedStyle(probe).backgroundColor;
    probe.remove();
    const pane = document.querySelector('section[aria-label="Item detail"]');
    return [...(pane?.querySelectorAll('button, a') ?? [])]
      .filter((el) => el.getClientRects().length > 0)
      .filter((el) => getComputedStyle(el).backgroundColor === accent)
      .map((el) => (el.textContent ?? '').trim());
  });
}

export async function checks({ browser, base, check, expect, sleep, sizes: { WIDTH, HEIGHT } }) {
  for (const width of [390, WIDTH]) {
    const height = width === WIDTH ? HEIGHT : 844;

    await check(`Plan a dispatch is on a queued item and on no item in review at ${width}px`, async () => {
      const ctx = await browser.newContext({ viewport: { width, height } });
      const page = await ctx.newPage();
      try {
        const reads = resolveReads(page);
        await openItem(page, base, '2055', sleep);
        expect((await launchpads(page)) === 0, `${width}px: #2055 (in review) shows Plan a dispatch`);
        await openItem(page, base, 'SFD-229', sleep);
        expect((await launchpads(page)) === 0, `${width}px: SFD-229 (held) shows Plan a dispatch`);
        expect(reads.length === 0, `${width}px: a plan was read for ${reads.join(', ')}`);

        await openItem(page, base, QUEUED, sleep);
        const panel = page.locator('section[aria-label="Plan a dispatch"]');
        await panel.waitFor({ state: 'visible', timeout: 15_000 });
        expect(reads.includes(QUEUED), `${width}px: the queued item's plan was never read`);
      } finally {
        await page.close();
        await ctx.close();
      }
    });

    await check(`a stalled-epic row has no launchpad and no palette dispatch at ${width}px`, async () => {
      const ctx = await browser.newContext({ viewport: { width, height } });
      const page = await ctx.newPage();
      try {
        await routeDashboard(page, withStalledEpic);
        const reads = resolveReads(page);
        await openItem(page, base, STALLED, sleep);
        expect((await launchpads(page)) === 0, `${width}px: ${STALLED} shows Plan a dispatch`);

        await page.keyboard.press('Control+k');
        const dialog = page.locator('[role="dialog"][aria-label="Go to"]');
        await dialog.waitFor({ state: 'visible', timeout: 5_000 });
        await sleep(SETTLE_MS);
        const options = await dialog
          .locator('[role="option"]')
          .evaluateAll((els) => els.map((el) => (el.textContent ?? '').trim()));
        expect(
          !options.some((text) => /kew dispatch/.test(text)),
          `${width}px: the palette offers a dispatch for ${STALLED}`
        );
        expect(reads.length === 0, `${width}px: a plan was read for ${reads.join(', ')}`);
        await page.keyboard.press('Escape');
        await dialog.waitFor({ state: 'detached', timeout: 5_000 });

        // The control: on the queued item, the same palette does read one.
        await openItem(page, base, QUEUED, sleep);
        const before = reads.length;
        await page.keyboard.press('Control+k');
        await dialog.waitFor({ state: 'visible', timeout: 5_000 });
        await sleep(SETTLE_MS);
        expect(reads.length > before, `${width}px: the palette read no plan for ${QUEUED}`);
      } finally {
        await page.close();
        await ctx.close();
      }
    });

    await check(`the Work detail has one magenta at most and a named model at ${width}px`, async () => {
      const ctx = await browser.newContext({ viewport: { width, height } });
      const page = await ctx.newPage();
      try {
        // A queued item and nobody waiting: nothing needs a person, so no
        // control is magenta — the closed line's Run and Copy, and the
        // drawer's, are quiet (#3467 D29; DESIGN.md `button-primary`).
        await openItem(page, base, QUEUED, sleep);
        const line = page.locator('section[aria-label="Plan a dispatch"] [data-plan-summary]');
        await line.filter({ hasText: ' / ' }).waitFor({ state: 'visible', timeout: 15_000 });
        await sleep(SETTLE_MS);
        let magenta = await magentaButtons(page);
        expect(magenta.length === 0, `${width}px: ${QUEUED} paints [${magenta.join(', ')}] magenta`);
        const runs = await line.locator('button[data-run-button]').count();
        expect(runs === 1, `${width}px: the closed line draws ${runs} Run button(s), not one`);
        const details = await line
          .locator('button')
          .filter({ hasText: 'Details' })
          .evaluate((el) => getComputedStyle(el).backgroundColor);
        expect(
          details === 'rgba(0, 0, 0, 0)' || details === 'transparent',
          `${width}px: Details is filled (${details})`
        );
        // The drawer: its Copy and Run are quiet too. Read before the
        // route checks below, which rely on the closed line.
        await line.locator('button').filter({ hasText: 'Details' }).click();
        const drawerRun = page.locator('[role="dialog"] button[data-drawer-run]').first();
        await drawerRun.waitFor({ state: 'visible', timeout: 15_000 });
        await sleep(SETTLE_MS);
        const drawn = await page.evaluate(() => {
          const probe = document.createElement('button');
          probe.className = 'bg-accent';
          document.body.append(probe);
          const accent = getComputedStyle(probe).backgroundColor;
          probe.remove();
          const dialog = document.querySelector('[role="dialog"]');
          const all = [...(dialog?.querySelectorAll('button, a') ?? [])].filter((el) => el.getClientRects().length > 0);
          return {
            copy: all.some((el) => el.getAttribute('aria-label') === 'Copy command'),
            magenta: all.filter((el) => getComputedStyle(el).backgroundColor === accent).map((el) => (el.textContent ?? '').trim())
          };
        });
        expect(drawn.copy, `${width}px: the drawer draws no Copy to read`);
        expect(drawn.magenta.length === 0, `${width}px: the drawer paints [${drawn.magenta.join(', ')}] magenta`);
        await page.keyboard.press('Escape');
        await page.locator('[role="dialog"]').first().waitFor({ state: 'detached', timeout: 5_000 }).catch(() => {});

        // The route names the model; the id is its title.
        const route = await line.locator('span[title]').first().evaluate((el) => ({
          text: (el.textContent ?? '').trim(),
          title: el.getAttribute('title') ?? ''
        }));
        expect(route.title.includes('gpt-5.6-sol'), `${width}px: the route's title is "${route.title}"`);
        expect(
          !route.text.includes('gpt-5.6-sol') && route.text.includes('GPT-5.6 Sol'),
          `${width}px: the closed line reads "${route.text}", not a model name`
        );

        // The same item with a needs-you block above: the block keeps the one.
        await routeDashboard(page, withQueuedWait);
        await openItem(page, base, QUEUED, sleep);
        await line.filter({ hasText: ' / ' }).waitFor({ state: 'visible', timeout: 15_000 });
        await sleep(SETTLE_MS);
        magenta = await magentaButtons(page);
        expect(
          magenta.length <= 1,
          `${width}px: ${QUEUED} under a needs-you block paints [${magenta.join(', ')}] magenta`
        );
        const launchRun = await line
          .locator('button[data-run-button]')
          .evaluate((el) => getComputedStyle(el).backgroundColor)
          .catch(() => null);
        expect(
          launchRun === null || launchRun === 'rgba(0, 0, 0, 0)',
          `${width}px: the launchpad's Run is filled (${launchRun}) under a needs-you block`
        );

        // And on the in-review and held items, never more than one.
        for (const item of ['2055', 'SFD-229']) {
          await openItem(page, base, item, sleep);
          magenta = await magentaButtons(page);
          expect(magenta.length <= 1, `${width}px: ${item} paints [${magenta.join(', ')}] magenta`);
        }
      } finally {
        await page.close();
        await ctx.close();
      }
    });
  }
}
