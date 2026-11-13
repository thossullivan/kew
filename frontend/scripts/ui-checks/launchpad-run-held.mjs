/**
 * A Run whose POST is still open, or whose answer was lost, outlives the
 * launchpad's button and holds the route it carries (#2975).
 *
 * One surface of `npm run ui-check`; `scripts/ui-check.mjs` runs it (#2570).
 */
// ui-check-owns: src/lib/work/DispatchLaunchpad.svelte
// ui-check-owns: src/lib/RunButton.svelte
// ui-check-owns: src/lib/run-request.ts
// ui-check-owns: src/routes/(factory)/work/+page.*
// ui-check-owns: src/routes/(factory)/monitor/+page.*
import { SETTLE_MS } from '../ui-check-lib.mjs';

function request(state) {
  return {
    id: 'ui-check-held',
    command: 'kew dispatch 2060',
    operator: 'ui-check',
    requested_at: '2026-10-16T12:00:00+00:00',
    source: 'web',
    state,
    item_id: '2060',
    drive: null,
    host: null,
    pid: null,
    log: null,
    started_at: null,
    finished_at: null,
    exit_code: null,
    detail: null,
    model: null
  };
}

/** Read `locator`'s label until it is `want` or six seconds pass. */
async function labelUntil(locator, want, sleep) {
  let got = '';
  for (let i = 0; i < 60; i++) {
    got = ((await locator.textContent()) ?? '').trim();
    if (got === want) break;
    await sleep(100);
  }
  return got;
}

/** Serve `/api/run-requests`: the ledger reads `ledger()`, a POST `post(route)`. */
async function serveRuns(page, ledger, post) {
  await page.route('**/api/run-requests**', async (route) => {
    if (route.request().method() === 'POST') return post(route);
    const url = new URL(route.request().url());
    const rows = ledger();
    if (url.pathname.endsWith('/run-requests')) return route.fulfill({ json: { requests: rows } });
    return route.fulfill(rows.length > 0 ? { json: rows[0] } : { status: 404, json: { detail: 'no such run request' } });
  });
}

/** Another ready-queue item, in the same list as #2060. */
const OTHER = '2058';

/** Open the drawer and pick `value` in the lane select; the drawer stays open. */
async function chooseLane(page, panel, value) {
  await panel.getByRole('button', { name: 'Details' }).click();
  const lane = page.getByRole('dialog', { name: /Plan a dispatch/ }).locator('#plan-lane');
  await lane.waitFor({ state: 'visible', timeout: 15_000 });
  await lane.selectOption(value);
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
 * Press every Copy surface — the summary line's and the open drawer's — with
 * the drawer still open, forcing past a disabled control.
 */
async function pressEveryCopy(page, panel) {
  const drawer = page.getByRole('dialog', { name: /Plan a dispatch/ });
  const surfaces = [
    panel.locator('[data-plan-summary]').getByRole('button', { name: 'Copy command' }),
    drawer.getByRole('button', { name: 'Copy command' })
  ];
  const enabled = [];
  for (const copy of surfaces) {
    if ((await copy.count()) === 0) continue;
    if (await copy.isEnabled()) enabled.push(copy);
    await copy.click({ force: true });
  }
  return enabled.length;
}

async function closeDrawer(page) {
  const drawer = page.getByRole('dialog', { name: /Plan a dispatch/ });
  await drawer.getByRole('button', { name: 'Close' }).click();
  await drawer.waitFor({ state: 'hidden', timeout: 15_000 });
}

async function openWork(page, base) {
  await page.goto(`${base}/work?item=2060`, { waitUntil: 'load' });
  const panel = page.locator('section[aria-label="Plan a dispatch"]');
  await panel.waitFor({ state: 'visible', timeout: 15_000 });
  const button = panel.locator('button[data-run-button]');
  await button.waitFor({ state: 'visible', timeout: 15_000 });
  return { panel, button };
}

export async function checks({ browser, base, check, expect, sleep, sizes: { WIDTH, HEIGHT } }) {
  /**
   * #2975 — the POST commits on the server whatever the page does meanwhile.
   * With it held: the lane is fixed, the operator goes to Monitor and comes
   * back by client navigation (the launchpad and its button unmount and mount
   * again), and the new button still reads "Sending…" and sends nothing. The
   * answer then lands in that button, and exactly one POST was made.
   */
  for (const width of [390, WIDTH]) {
    await check(
      `a held POST survives launchpad navigation, renders Sending, reconciles its response, and sends exactly one POST at ${width}px`,
      async () => {
        const ctx = await browser.newContext({ viewport: { width, height: width === WIDTH ? HEIGHT : 844 } });
        const page = await ctx.newPage();
        try {
          let current = [];
          let posts = 0;
          let answer = null;
          await serveRuns(
            page,
            () => current,
            async (route) => {
              posts += 1;
              const reply = await new Promise((resolve) => (answer = resolve));
              return route.fulfill(reply);
            }
          );
          let { panel, button } = await openWork(page, base);
          await sleep(SETTLE_MS);

          await button.click();
          for (let i = 0; i < 50 && answer === null; i++) await sleep(100);
          expect(answer !== null, `${width}px: pressing Run sent no POST`);
          const sending = await labelUntil(button, 'Sending…', sleep);
          expect(sending === 'Sending…', `${width}px: with the POST open, Run reads "${sending}"`);

          // The route is held: the lane cannot be changed under the open POST.
          await panel.getByRole('button', { name: 'Details' }).click();
          const drawer = page.getByRole('dialog', { name: /Plan a dispatch/ });
          const lane = drawer.locator('#plan-lane');
          await lane.waitFor({ state: 'visible', timeout: 15_000 });
          expect(await lane.isDisabled(), `${width}px: with the POST open, the lane select is enabled`);
          await drawer.getByRole('button', { name: 'Close' }).click();
          await drawer.waitFor({ state: 'hidden', timeout: 15_000 });

          // Away and back, client-side: the launchpad unmounts and mounts again.
          const monitor = page.locator('nav[aria-label="Factory views"] a', { hasText: 'Monitor' });
          await monitor.click();
          await page.waitForURL(/\/monitor/, { timeout: 15_000 });
          await panel.waitFor({ state: 'detached', timeout: 15_000 });
          await page.goBack();
          await page.waitForURL(/\/work\?item=2060/, { timeout: 15_000 });
          ({ panel, button } = {
            panel: page.locator('section[aria-label="Plan a dispatch"]'),
            button: page.locator('section[aria-label="Plan a dispatch"] button[data-run-button]')
          });
          await button.waitFor({ state: 'visible', timeout: 15_000 });
          const back = await labelUntil(button, 'Sending…', sleep);
          expect(back === 'Sending…', `${width}px: back on the item mid-POST, Run reads "${back}", not "Sending…"`);
          expect(!(await button.isEnabled()), `${width}px: back on the item mid-POST, Run is pressable`);
          await button.click({ force: true });
          await sleep(SETTLE_MS);
          expect(posts === 1, `${width}px: a press on the remounted button sent ${posts} POSTs, not one`);

          current = [request('queued')];
          answer({ json: { request: current[0], created: true, drive: { state: 'running', running: true, reason: null } } });
          const queued = await labelUntil(button, 'Queued…', sleep);
          expect(queued === 'Queued…', `${width}px: once the held POST answers, Run reads "${queued}", not "Queued…"`);
          const said = ((await panel.locator('[data-run-line]').textContent()) ?? '').trim();
          expect(said.startsWith('queued'), `${width}px: after the POST the line reads "${said}", not the queued request`);
          expect(posts === 1, `${width}px: the Run made ${posts} POSTs, not one`);
        } finally {
          await ctx.close();
        }
      }
    );
  }

  /**
   * #2975 — Copy hands over the same command Run sends, and a pasted copy runs
   * beside the request a held POST is still making. So while the POST is open
   * both Copy surfaces are shut, and a press forced past them writes nothing
   * to the clipboard; once the drive settles the request, Copy is back.
   */
  await check('a held POST disables both Copy surfaces and their clipboard writes', async () => {
    const ctx = await browser.newContext({ viewport: { width: WIDTH, height: HEIGHT } });
    const page = await ctx.newPage();
    try {
      let current = [];
      let answer = null;
      await serveRuns(
        page,
        () => current,
        async (route) => route.fulfill(await new Promise((resolve) => (answer = resolve)))
      );
      const copied = await spyClipboard(page);
      const { panel, button } = await openWork(page, base);
      await sleep(SETTLE_MS);
      expect(await button.isEnabled(), 'with nothing queued, Run is not pressable — the check proves nothing');

      await button.click();
      for (let i = 0; i < 50 && answer === null; i++) await sleep(100);
      expect(answer !== null, 'pressing Run sent no POST');
      await panel.getByRole('button', { name: 'Details' }).click();
      const drawer = page.getByRole('dialog', { name: /Plan a dispatch/ });
      await drawer.waitFor({ state: 'visible', timeout: 15_000 });
      const surfaces =
        (await panel.locator('[data-plan-summary]').getByRole('button', { name: 'Copy command' }).count()) +
        (await drawer.getByRole('button', { name: 'Copy command' }).count());
      expect(surfaces === 2, `with the POST open, ${surfaces} Copy surfaces are on screen, not two`);
      const live = await pressEveryCopy(page, panel);
      await sleep(SETTLE_MS);
      expect(live === 0, `with the POST open, ${live} Copy controls are enabled`);
      expect((await copied()).length === 0, `with the POST open, Copy handed over ${JSON.stringify(await copied())}`);
      await closeDrawer(page);

      current = [{ ...request('done'), exit_code: 0 }];
      answer({ json: { request: current[0], created: true, drive: { state: 'running', running: true, reason: null } } });
      const copy = panel.locator('[data-plan-summary]').getByRole('button', { name: 'Copy command' });
      for (let i = 0; i < 60 && !(await copy.isEnabled()); i++) await sleep(100);
      expect(await copy.isEnabled(), 'once the drive settled the request, Copy is still shut');
      await copy.click();
      await sleep(SETTLE_MS);
      expect((await copied()).length === 1, `once settled, Copy handed over ${JSON.stringify(await copied())}`);
    } finally {
      await ctx.close();
    }
  });

  /**
   * #2975 — until the ledger answers for the pair on screen, the page cannot
   * know a request of it is not already queued: Run and both Copies wait for
   * that read. It is held here with the drawer open.
   */
  await check('a held ledger lookup disables Run and both Copy surfaces', async () => {
    const ctx = await browser.newContext({ viewport: { width: WIDTH, height: HEIGHT } });
    const page = await ctx.newPage();
    try {
      let posts = 0;
      let holding = true;
      const held = [];
      await page.route('**/api/run-requests**', async (route) => {
        if (route.request().method() === 'POST') {
          posts += 1;
          return route.fulfill({ json: { request: request('queued'), created: true } });
        }
        const url = new URL(route.request().url());
        if (holding && url.searchParams.get('command') === 'kew dispatch 2060')
          await new Promise((resolve) => held.push(resolve));
        return route.fulfill({ json: { requests: [] } });
      });
      const copied = await spyClipboard(page);
      const { panel, button } = await openWork(page, base);
      for (let i = 0; i < 50 && held.length === 0; i++) await sleep(100);
      expect(held.length > 0, 'the launchpad sent no ledger read — the check proves nothing');
      await panel.getByRole('button', { name: 'Details' }).click();
      await page.getByRole('dialog', { name: /Plan a dispatch/ }).waitFor({ state: 'visible', timeout: 15_000 });
      await sleep(SETTLE_MS);

      const live = await pressEveryCopy(page, panel);
      await sleep(SETTLE_MS);
      expect(live === 0, `with the ledger read held, ${live} Copy controls are enabled`);
      expect((await copied()).length === 0, `with the ledger read held, Copy handed over ${JSON.stringify(await copied())}`);
      await closeDrawer(page);
      expect(!(await button.isEnabled()), 'with the ledger read held, Run is pressable');
      await button.click({ force: true });
      await sleep(SETTLE_MS);
      expect(posts === 0, `a press while the ledger read was held sent ${posts} POSTs`);

      holding = false;
      for (const release of held.splice(0)) release();
      for (let i = 0; i < 60 && !(await button.isEnabled()); i++) await sleep(100);
      expect(await button.isEnabled(), 'once the ledger read answered empty, Run is still shut');
      const copy = panel.locator('[data-plan-summary]').getByRole('button', { name: 'Copy command' });
      expect(await copy.isEnabled(), 'once the ledger read answered empty, Copy is still shut');
    } finally {
      await ctx.close();
    }
  });

  /**
   * #2975 — a ledger read that fails (a 5xx, a body that is not a list) is
   * not an empty ledger. Run and both Copies stay shut, the read is asked
   * again on the follow's beat, and only an answer frees them.
   */
  await check('failed ledger lookup keeps Run disabled and retries before enabling', async () => {
    const ctx = await browser.newContext({ viewport: { width: WIDTH, height: HEIGHT } });
    const page = await ctx.newPage();
    try {
      let posts = 0;
      let reads = 0;
      await page.route('**/api/run-requests**', async (route) => {
        if (route.request().method() === 'POST') {
          posts += 1;
          return route.fulfill({ json: { request: request('queued'), created: true } });
        }
        const url = new URL(route.request().url());
        if (url.searchParams.get('command') !== 'kew dispatch 2060') return route.fulfill({ json: { requests: [] } });
        reads += 1;
        if (reads === 1) return route.fulfill({ status: 503, json: { detail: 'the ledger is busy' } });
        if (reads === 2) return route.fulfill({ json: { requests: null } });
        return route.fulfill({ json: { requests: [] } });
      });
      const copied = await spyClipboard(page);
      const { panel, button } = await openWork(page, base);
      for (let i = 0; i < 50 && reads === 0; i++) await sleep(100);
      expect(reads > 0, 'the launchpad sent no ledger read — the check proves nothing');
      await sleep(SETTLE_MS);

      expect(!(await button.isEnabled()), 'after a failed ledger read, Run is pressable');
      await button.click({ force: true });
      await panel.getByRole('button', { name: 'Details' }).click();
      await page.getByRole('dialog', { name: /Plan a dispatch/ }).waitFor({ state: 'visible', timeout: 15_000 });
      const live = await pressEveryCopy(page, panel);
      await sleep(SETTLE_MS);
      expect(live === 0, `after a failed ledger read, ${live} Copy controls are enabled`);
      expect((await copied()).length === 0, `after a failed ledger read, Copy handed over ${JSON.stringify(await copied())}`);
      expect(posts === 0, `a press after a failed ledger read sent ${posts} POSTs`);
      await closeDrawer(page);

      for (let i = 0; i < 150 && !(await button.isEnabled()); i++) await sleep(100);
      expect(reads >= 3, `the failed ledger read was asked ${reads - 1} more times, not until it answered`);
      expect(await button.isEnabled(), 'once the ledger read answered empty, Run is still shut');
      expect(posts === 0, `the failed ledger read ended with ${posts} POSTs`);
    } finally {
      await ctx.close();
    }
  });

  /**
   * #2975 — a Run accepted while its button listened is in the ledger, not
   * the page's memory: the answer was delivered, so a button mounted later
   * learns of it only from its ledger read. Until that read lands the button
   * cannot know the pair is taken, so it stays shut: a press then would send a
   * second POST for a request still queued. The read is held here, after a
   * client-side remount, and a forced press must send nothing.
   */
  await check('a Run accepted before a remount keeps the new Run shut while its ledger read is held, and sends no second POST', async () => {
    const ctx = await browser.newContext({ viewport: { width: WIDTH, height: HEIGHT } });
    const page = await ctx.newPage();
    try {
      let current = [];
      let posts = 0;
      let holdReads = false;
      const held = [];
      await page.route('**/api/run-requests**', async (route) => {
        if (route.request().method() === 'POST') {
          posts += 1;
          current = [request('queued')];
          return route.fulfill({
            json: { request: current[0], created: true, drive: { state: 'running', running: true, reason: null } }
          });
        }
        if (new URL(route.request().url()).pathname.endsWith('/run-requests')) {
          if (holdReads) await new Promise((resolve) => held.push(resolve));
          return route.fulfill({ json: { requests: current } });
        }
        return route.fulfill({ json: current[0] ?? request('queued') });
      });
      let { button } = await openWork(page, base);
      await sleep(SETTLE_MS);
      await button.click();
      const accepted = await labelUntil(button, 'Queued…', sleep);
      expect(accepted === 'Queued…', `once its POST answered, Run reads "${accepted}", not "Queued…"`);

      // Away and back, client-side, with the new button's ledger read held.
      holdReads = true;
      const panel = page.locator('section[aria-label="Plan a dispatch"]');
      await page.locator('nav[aria-label="Factory views"] a', { hasText: 'Monitor' }).click();
      await page.waitForURL(/\/monitor/, { timeout: 15_000 });
      await panel.waitFor({ state: 'detached', timeout: 15_000 });
      await page.goBack();
      await page.waitForURL(/\/work\?item=2060/, { timeout: 15_000 });
      button = panel.locator('button[data-run-button]');
      await button.waitFor({ state: 'visible', timeout: 15_000 });
      for (let i = 0; i < 50 && held.length === 0; i++) await sleep(100);
      expect(held.length > 0, 'the remounted Run sent no ledger read — the check proves nothing');
      expect(!(await button.isEnabled()), 'with its ledger read held, the remounted Run is pressable');
      await button.click({ force: true });
      await sleep(SETTLE_MS);
      expect(posts === 1, `a press while the ledger read was held sent ${posts} POSTs, not one`);

      holdReads = false;
      for (const release of held.splice(0)) release();
      const back = await labelUntil(button, 'Queued…', sleep);
      expect(back === 'Queued…', `once the ledger read landed, Run reads "${back}", not "Queued…"`);
      expect(posts === 1, `the Run made ${posts} POSTs, not one`);
    } finally {
      await ctx.close();
    }
  });

  /**
   * #2975 — a POST whose answer is lost (the connection dropped) may have
   * queued the Run, or be about to: the server can still be writing it when
   * anything else asks. The ledger here never shows it, so a ledger read would
   * find nothing and give Run back. The console instead asks the same POST
   * again with the same key, and the server (as
   * `kew.service.run_requests.queue_request` does) gives every attempt with
   * that key the request the key was written with — first a 503 (the ledger
   * busy), then the request, in whatever state it reached meanwhile. Run stays
   * held through it all, and only one press's key is ever sent.
   *
   * A 2xx naming no request is not lost: the handler finished, so it is
   * reconciled against the ledger instead (issue #2975, design item 3) —
   * `run-queued-label.mjs`, "a 2xx naming no request gives Run back".
   */
  for (const lost of ['an aborted']) {
    for (const reached of ['queued', 'failed']) {
      await check(
        `an ambiguous POST with ${lost} response, delayed ledger visibility and a request ${reached} before the next read is not resubmitted`,
        async () => {
          const ctx = await browser.newContext({ viewport: { width: WIDTH, height: HEIGHT } });
          const page = await ctx.newPage();
          try {
            const keys = [];
            let deciding = true;
            await serveRuns(
              page,
              () => [],
              (route) => {
                keys.push(route.request().postDataJSON().key);
                if (keys.length === 1) return route.abort('connectionreset');
                if (deciding)
                  return route.fulfill({ status: 503, json: { detail: 'the ledger is busy — ask again' } });
                const row = { ...request(reached), exit_code: reached === 'failed' ? 3 : null, detail: reached === 'failed' ? 'boom' : null };
                return route.fulfill({ json: { request: row, created: false } });
              }
            );
            const { panel, button } = await openWork(page, base);
            await sleep(SETTLE_MS);

            await button.click();
            for (let i = 0; i < 80 && keys.length < 2; i++) await sleep(100);
            expect(keys.length >= 2, `${lost} POST answer was not asked again (${keys.length} POSTs)`);
            const held = await labelUntil(button, 'Sending…', sleep);
            expect(held === 'Sending…', `${lost} POST answer, still deciding: Run reads "${held}", not "Sending…"`);
            expect(!(await button.isEnabled()), `${lost} POST answer, still deciding: Run is pressable again`);
            await button.click({ force: true });

            deciding = false;
            const want = reached === 'queued' ? 'Queued…' : 'Run';
            const line = reached === 'queued' ? 'queued' : 'failed (exit 3) — boom';
            let said = '';
            for (let i = 0; i < 100; i++) {
              said = ((await panel.locator('[data-run-line]').textContent().catch(() => '')) ?? '').trim();
              if (said.startsWith(line)) break;
              await sleep(100);
            }
            expect(said.startsWith(line), `${lost} POST answer leaves the line reading "${said}", not the ${reached} request`);
            const label = await labelUntil(button, want, sleep);
            expect(label === want, `${lost} POST answer, ${reached}: Run reads "${label}", not "${want}"`);
            expect(
              keys.every((k) => typeof k === 'string' && k === keys[0]),
              `${lost} POST answer: the attempts sent ${new Set(keys).size} different keys, not one press's`
            );
          } finally {
            await ctx.close();
          }
        }
      );
    }
  }

  /**
   * #2975 — a lane change takes a plan read, and until it lands the Run on
   * screen is the old route's. A press then would send the stale command, and
   * the new plan would unmount its button mid-POST. So Run is held while the
   * route resolves; the review lane it resolves to has no dispatch command, so
   * the implement Run then goes.
   */
  await check('a held route resolution disables stale Run and cannot hide a later POST response', async () => {
    const ctx = await browser.newContext({ viewport: { width: WIDTH, height: HEIGHT } });
    const page = await ctx.newPage();
    try {
      let posts = 0;
      let reads = 0;
      let release = null;
      await serveRuns(
        page,
        () => [],
        (route) => {
          posts += 1;
          return route.fulfill({ json: { request: request('queued'), created: true } });
        }
      );
      await page.route('**/api/dispatch/resolve**', async (route) => {
        reads += 1;
        if (reads === 1) return route.fallback();
        await new Promise((resolve) => (release = resolve));
        const response = await route.fetch();
        const body = await response.json();
        body.lane = { ...body.lane, value: 'review' };
        return route.fulfill({ response, json: body });
      });
      const copied = await spyClipboard(page);
      const { panel, button } = await openWork(page, base);
      await sleep(SETTLE_MS);
      expect(await button.isEnabled(), 'with the route resolved, Run is not pressable — the control proves nothing');
      await panel.locator('[data-plan-summary]').getByRole('button', { name: 'Copy command' }).click();
      await sleep(SETTLE_MS);
      expect((await copied()).length === 1, 'with the route resolved, Copy copied nothing — the spy proves nothing');

      await chooseLane(page, panel, 'review');
      for (let i = 0; i < 50 && release === null; i++) await sleep(100);
      expect(release !== null, 'a lane change sent no plan read');
      const live = await pressEveryCopy(page, panel);
      await sleep(SETTLE_MS);
      expect(live === 0, `with the route resolving, ${live} Copy controls are still enabled`);
      expect(
        (await copied()).length === 1,
        `with the route resolving, Copy handed over ${JSON.stringify((await copied()).slice(1))}`
      );
      await closeDrawer(page);

      expect(!(await button.isEnabled()), 'with the route resolving, the stale Run is pressable');
      await button.click({ force: true });
      await sleep(SETTLE_MS);
      expect(posts === 0, `a press during the route read sent ${posts} POSTs`);

      release();
      await button.waitFor({ state: 'detached', timeout: 6_000 }).catch(() => {});
      expect((await button.count()) === 0, 'once the review lane resolved, the implement Run is still on screen');
      expect(posts === 0, `the lane change ended with ${posts} POSTs`);
    } finally {
      await ctx.close();
    }
  });

  /**
   * #2975 — a lane change whose read fails keeps the last good plan in the
   * drawer, but that plan is the implement lane's: its Run would launch the
   * lane the operator moved off. So neither Run nor Copy is offered for it.
   */
  await check('failed review-lane resolution keeps stale Run unavailable and sends no POST', async () => {
    const ctx = await browser.newContext({ viewport: { width: WIDTH, height: HEIGHT } });
    const page = await ctx.newPage();
    try {
      let posts = 0;
      let reads = 0;
      await serveRuns(
        page,
        () => [],
        (route) => {
          posts += 1;
          return route.fulfill({ json: { request: request('queued'), created: true } });
        }
      );
      await page.route('**/api/dispatch/resolve**', async (route) => {
        reads += 1;
        return reads === 1 ? route.fallback() : route.abort('connectionreset');
      });
      const copied = await spyClipboard(page);
      const { panel, button } = await openWork(page, base);
      await sleep(SETTLE_MS);
      expect(await button.isEnabled(), 'with the route resolved, Run is not pressable — the control proves nothing');

      await chooseLane(page, panel, 'review');
      const drawer = page.getByRole('dialog', { name: /Plan a dispatch/ });
      let said = '';
      for (let i = 0; i < 60 && !said.includes('could not be resolved'); i++) {
        said = (await drawer.textContent()) ?? '';
        await sleep(100);
      }
      expect(reads === 2, `the lane change took ${reads - 1} plan reads, not one`);
      expect(said.includes('could not be resolved'), 'the failed lane read is not said in the drawer');
      const live = await pressEveryCopy(page, panel);
      await sleep(SETTLE_MS);
      expect(live === 0, `after the review read failed, ${live} Copy controls are still enabled`);
      expect(
        (await copied()).length === 0,
        `after the review read failed, Copy handed over ${JSON.stringify(await copied())}`
      );
      await closeDrawer(page);

      expect((await button.count()) === 0, 'after the review read failed, the implement Run is still on screen');
      expect(
        (await panel.getByRole('button', { name: 'Copy command' }).count()) === 0,
        "after the review read failed, Copy still offers the implement lane's command"
      );
      await sleep(SETTLE_MS);
      expect(posts === 0, `the failed lane change ended with ${posts} POSTs`);
    } finally {
      await ctx.close();
    }
  });

  /**
   * #2975 — the launchpad stays mounted when another Work item is selected.
   * A Run held for #2060 must not leave its "sending" line on the next item's
   * panel — here one with no dispatch command, so no Run mounts there to
   * replace the line — and its answer, landing while the operator is away, is
   * #2060's to show when the operator is back.
   */
  await check('switching Work items scopes a held Run line to its originating item', async () => {
    const ctx = await browser.newContext({ viewport: { width: WIDTH, height: HEIGHT } });
    const page = await ctx.newPage();
    try {
      let current = [];
      let posts = 0;
      let answer = null;
      await serveRuns(
        page,
        () => current,
        async (route) => {
          posts += 1;
          return route.fulfill(await new Promise((resolve) => (answer = resolve)));
        }
      );
      await page.route('**/api/dispatch/resolve**', async (route) => {
        if (new URL(route.request().url()).searchParams.get('item') !== OTHER) return route.fallback();
        const response = await route.fetch();
        const body = await response.json();
        return route.fulfill({ response, json: { ...body, issue_num: null } });
      });
      const { panel, button } = await openWork(page, base);
      await sleep(SETTLE_MS);
      await button.click();
      for (let i = 0; i < 50 && answer === null; i++) await sleep(100);
      expect(answer !== null, 'pressing Run sent no POST');
      const line = panel.locator('[data-run-line]');
      const sending = ((await line.textContent()) ?? '').trim();
      expect(sending.startsWith('sending'), `with the POST open, #2060's line reads "${sending}"`);

      await page.locator(`aside[aria-label="Issues"] a[data-item="${OTHER}"]`).first().click();
      await page.waitForURL(new RegExp(`item=${OTHER}`), { timeout: 15_000 });
      await sleep(SETTLE_MS);
      expect((await panel.count()) === 1, `#${OTHER} shows no Plan a dispatch — the check proves nothing`);
      expect((await button.count()) === 0, `#${OTHER}'s plan has no command, yet a Run is on its panel`);
      const away = (await line.count()) === 0 ? '' : ((await line.textContent()) ?? '').trim();
      expect(away === '', `#${OTHER}'s panel reads #2060's Run line "${away}"`);

      current = [request('queued')];
      answer({ json: { request: current[0], created: true, drive: { state: 'running', running: true, reason: null } } });
      await sleep(SETTLE_MS);
      const after = (await line.count()) === 0 ? '' : ((await line.textContent()) ?? '').trim();
      expect(after === '', `#2060's answer landed on #${OTHER}'s panel as "${after}"`);

      await page.locator('aside[aria-label="Issues"] a[data-item="2060"]').first().click();
      await page.waitForURL(/item=2060/, { timeout: 15_000 });
      await button.waitFor({ state: 'visible', timeout: 15_000 });
      const queued = await labelUntil(button, 'Queued…', sleep);
      expect(queued === 'Queued…', `back on #2060, Run reads "${queued}", not "Queued…"`);
      const back = ((await line.textContent()) ?? '').trim();
      expect(back.startsWith('queued'), `back on #2060, its line reads "${back}", not the queued request`);
      expect(posts === 1, `the Run made ${posts} POSTs, not one`);
    } finally {
      await ctx.close();
    }
  });
}
