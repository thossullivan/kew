/**
 * /work draws its shell while the reading is out (#2545 D9).
 *
 * One surface of `npm run ui-check`; `scripts/ui-check.mjs` runs it (#2570).
 */
// ui-check-owns: src/routes/(factory)/work/+page.ts
// ui-check-owns: src/routes/(factory)/work/+page.svelte

export async function checks({ browser, base, check, expect, sizes: { WIDTH, HEIGHT } }) {
  /**
   * #2545 D9 — /work draws its shell while the work reading is still out.
   *
   * The loader used to await both reads, and a client-rendered app draws
   * nothing at all until its loaders resolve: on a live server that was one to
   * three seconds of blank page, header included. Every dashboard read the
   * page makes (`/api/dashboard`, which the shell polls, and
   * `/api/dashboard/work`, the work projection) is held here on a promise this
   * check releases, so the property is the order (#2909): the header and the
   * loading sentence draw while the work read is still held, and only then is
   * it let go. No clock decides it — a shell that awaits the read never draws
   * while held, so the wait for it fails.
   *
   * The route matches on the path, not a glob: `**` + `/api/dashboard*` does
   * not cross the `/` into `/api/dashboard/work`, so the work read went out
   * unheld and its answer was counted against the shell. Only the requests
   * the route holds are counted.
   */
  const DRAW_WAIT_MS = 15_000;
  const WORK_READ = '/api/dashboard/work';
  const isDashboardRead = (url) => url.pathname === '/api/dashboard' || url.pathname.startsWith('/api/dashboard/');
  for (const width of [WIDTH, 390]) {
    await check(`/work draws its header and "Reading work items…" while the work read is held, at ${width}px`, async () => {
      const ctx = await browser.newContext({ viewport: { width, height: width === WIDTH ? HEIGHT : 844 } });
      const page = await ctx.newPage();
      const problems = [];
      page.on('pageerror', (error) => problems.push(`page error: ${error.message}`));
      const heldRequests = new Set();
      let answered = 0;
      let release;
      const held = new Promise((resolve) => {
        release = resolve;
      });
      let workHeld;
      const workAsked = new Promise((resolve) => {
        workHeld = resolve;
      });
      page.on('response', (response) => {
        if (heldRequests.has(response.request())) answered += 1;
      });
      await page.route(isDashboardRead, async (route) => {
        heldRequests.add(route.request());
        if (new URL(route.request().url()).pathname === WORK_READ) workHeld();
        await held;
        await route.continue().catch(() => {});
      });
      let guardTimer;
      try {
        await page.goto(`${base}/work`, { waitUntil: 'commit' });
        const tab = page.locator('nav[aria-label="Factory views"] a[href="/work"]');
        const sentence = page.getByText('Reading work items…', { exact: true });
        // A hang guard only: the route resolves `workAsked` as the read goes out.
        const guard = new Promise((_, reject) => {
          guardTimer = setTimeout(
            () => reject(new Error(`${width}px: hang guard: the page never asked for ${WORK_READ}`)),
            DRAW_WAIT_MS
          );
        });
        await Promise.all([
          tab.waitFor({ state: 'visible', timeout: DRAW_WAIT_MS }),
          sentence.waitFor({ state: 'visible', timeout: DRAW_WAIT_MS }),
          Promise.race([workAsked, guard])
        ]);
        expect(answered === 0, `${width}px: a held dashboard read was answered before the shell drew`);
        expect(await sentence.isVisible(), `${width}px: the loading sentence went while the work read was held`);
        // And the sentence gives way to the list once the reading lands.
        release();
        await sentence.waitFor({ state: 'detached', timeout: DRAW_WAIT_MS });
        await page.locator('aside[aria-label="Issues"] a[data-item]').first().waitFor({ state: 'visible', timeout: DRAW_WAIT_MS });
        expect(problems.length === 0, problems.join('\n'));
      } finally {
        clearTimeout(guardTimer);
        release();
        await page.close();
        await ctx.close();
      }
    });
  }
}
