/**
 * A needs-you block's Copy, and the page's `c`, hand over the command its Run
 * sends, so both wait on the same gate as Run (#2975): nothing is copied while
 * the pair's ledger lookup is out, a POST of it is open, or a request of it is
 * queued or running — however long ago that request was queued.
 *
 * One surface of `npm run ui-check`; `scripts/ui-check.mjs` runs it (#2570).
 */
// ui-check-owns: src/lib/needs-you/NeedsYouBlock.svelte
// ui-check-owns: src/routes/(factory)/work/+page.svelte
import { SETTLE_MS } from '../ui-check-lib.mjs';

/** SFD-229 is the fixture row whose needs-you block carries a runnable command. */
const ITEM = 'SFD-229';
const COMMAND = 'kew epic unhold go-live-2026-09 SFD-229';

function request(state, requestedAt = '2026-10-16T12:00:00+00:00') {
  return {
    id: 'ui-check-needs-you',
    command: COMMAND,
    operator: 'ui-check',
    requested_at: requestedAt,
    source: 'web',
    state,
    item_id: ITEM,
    drive: null,
    host: null,
    pid: null,
    log: null,
    started_at: state === 'running' ? requestedAt : null,
    finished_at: state === 'done' ? requestedAt : null,
    exit_code: state === 'done' ? 0 : null,
    detail: null,
    model: null
  };
}

/** Record what the page hands the clipboard, instead of a real clipboard. */
async function spyClipboard(page) {
  await page.addInitScript(() => {
    window.__copied = [];
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText: async (text) => void window.__copied.push(text) }
    });
  });
  return () => page.evaluate(() => window.__copied.slice());
}

/**
 * Serve `/api/run-requests`: a lookup answers `lookup(route)`, a read of one
 * request `current()`, a POST `post(route)`.
 */
async function serveRuns(page, { lookup, current, post }) {
  await page.route('**/api/run-requests**', async (route) => {
    if (route.request().method() === 'POST') return post(route);
    const url = new URL(route.request().url());
    if (url.pathname.endsWith('/run-requests')) return lookup(route);
    return route.fulfill({ json: current() });
  });
}

async function openBlock(page, base) {
  await page.goto(`${base}/work?item=${ITEM}`, { waitUntil: 'load' });
  const block = page.locator('section[aria-label^="Needs you"]').filter({ has: page.locator('code[data-command]') });
  await block.first().waitFor({ state: 'visible', timeout: 15_000 });
  const copy = block.first().getByRole('button', { name: 'Copy command (c)' });
  const run = block.first().locator('button[data-run-button]');
  await run.waitFor({ state: 'visible', timeout: 15_000 });
  return { copy, run };
}

/**
 * Try every way to copy: the block's Copy, forced past a disabled control, and
 * `c` with nothing focused. Whether Copy was enabled, and what was copied.
 */
async function tryCopy(page, copy, copied, sleep) {
  const before = (await copied()).length;
  const enabled = await copy.isEnabled();
  await copy.click({ force: true });
  await page.evaluate(() => /** @type {HTMLElement | null} */ (document.activeElement)?.blur());
  await page.keyboard.press('c');
  await sleep(SETTLE_MS);
  return { enabled, handed: (await copied()).slice(before) };
}

export async function checks({ browser, base, check, expect, sleep, sizes: { WIDTH, HEIGHT } }) {
  /**
   * #2975 — with the pair's lookup held, then its POST held, neither the
   * block's Copy nor `c` hands the command over; once the drive has settled
   * the request, both do.
   */
  await check('a delayed lookup and held POST disable NeedsYouBlock Copy and the c shortcut', async () => {
    const ctx = await browser.newContext({ viewport: { width: WIDTH, height: HEIGHT } });
    const page = await ctx.newPage();
    try {
      const copied = await spyClipboard(page);
      const lookups = [];
      let rows = [];
      let posts = 0;
      let answer = null;
      await serveRuns(page, {
        lookup: async (route) => {
          if (lookups.length === 0) {
            await new Promise((resolve) => lookups.push(resolve));
          } else lookups.push(null);
          return route.fulfill({ json: { requests: rows } });
        },
        current: () => rows[0] ?? request('done'),
        post: async (route) => {
          posts += 1;
          return route.fulfill(await new Promise((resolve) => (answer = resolve)));
        }
      });
      const { copy, run } = await openBlock(page, base);
      for (let i = 0; i < 50 && lookups.length === 0; i++) await sleep(100);
      expect(lookups.length === 1, `the block made ${lookups.length} ledger lookups before the first answered`);

      const out = await tryCopy(page, copy, copied, sleep);
      expect(!out.enabled, 'with the ledger lookup out, the block offers Copy');
      expect(out.handed.length === 0, `with the ledger lookup out, Copy or c handed over ${JSON.stringify(out.handed)}`);
      expect(!(await run.isEnabled()), 'with the ledger lookup out, Run is pressable');

      lookups[0]();
      for (let i = 0; i < 60 && !(await run.isEnabled()); i++) await sleep(100);
      expect(await run.isEnabled(), 'the empty lookup landed, yet Run stays shut — the check proves nothing');

      await run.click();
      for (let i = 0; i < 50 && answer === null; i++) await sleep(100);
      expect(answer !== null, 'pressing Run sent no POST');
      const held = await tryCopy(page, copy, copied, sleep);
      expect(!held.enabled, 'with the POST open, the block offers Copy');
      expect(held.handed.length === 0, `with the POST open, Copy or c handed over ${JSON.stringify(held.handed)}`);

      rows = [request('done')];
      answer({ json: { request: rows[0], created: true, drive: { state: 'live', running: true, reason: null } } });
      for (let i = 0; i < 60 && !(await copy.isEnabled()); i++) await sleep(100);
      const after = await tryCopy(page, copy, copied, sleep);
      expect(after.enabled, 'with the request settled, the block still holds Copy');
      expect(
        after.handed.length === 2 && after.handed.every((text) => text === COMMAND),
        `with the request settled, Copy and c handed over ${JSON.stringify(after.handed)}, not the command twice`
      );
      expect(posts === 1, `the Run made ${posts} POSTs, not one`);
    } finally {
      await ctx.close();
    }
  });

  /**
   * #2975 — the pair lookup returns the request holding the pair however old
   * it is. One still running eight days after it was queued keeps Run, Copy
   * and `c` shut, and Run reads its state.
   */
  await check('an active request older than LIST_WINDOW keeps Run and Copy disabled and displays its state', async () => {
    const ctx = await browser.newContext({ viewport: { width: WIDTH, height: HEIGHT } });
    const page = await ctx.newPage();
    try {
      const copied = await spyClipboard(page);
      const old = new Date(Date.now() - 8 * 24 * 3600 * 1000).toISOString();
      const running = request('running', old);
      let posts = 0;
      await serveRuns(page, {
        lookup: (route) => route.fulfill({ json: { requests: [running] } }),
        current: () => running,
        post: (route) => {
          posts += 1;
          return route.fulfill({ status: 409, json: { detail: 'already running', request: running } });
        }
      });
      const { copy, run } = await openBlock(page, base);
      let label = '';
      for (let i = 0; i < 60 && label !== 'Running…'; i++) {
        label = ((await run.textContent()) ?? '').trim();
        await sleep(100);
      }
      expect(label === 'Running…', `an eight-day-old running request reads "${label}" on Run, not "Running…"`);
      expect(!(await run.isEnabled()), 'with the pair running, Run is pressable');
      await run.click({ force: true });
      const out = await tryCopy(page, copy, copied, sleep);
      expect(!out.enabled, 'with the pair running, the block offers Copy');
      expect(out.handed.length === 0, `with the pair running, Copy or c handed over ${JSON.stringify(out.handed)}`);
      expect(posts === 0, `with the pair running, a forced Run made ${posts} POSTs`);
    } finally {
      await ctx.close();
    }
  });
}
