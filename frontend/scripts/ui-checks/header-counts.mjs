/**
 * The header's counts against the live ledger (#2566).
 *
 * One surface of `npm run ui-check`; `scripts/ui-check.mjs` runs it (#2570).
 */
// ui-check-owns: src/lib/shell/FactoryHeader.svelte
// ui-check-owns: src/lib/health.ts
// ui-check-owns: src/routes/(factory)/health/+page.svelte
import { SETTLE_MS } from '../ui-check-lib.mjs';

export async function checks({ browser, base, check, expect, sleep, sizes: { WIDTH, HEIGHT } }) {
  /**
   * #2566 — the header counts nothing for the live ledger's last three readings.
   *
   * After #2558 the live header read "3 down", and none was a failure: the
   * serve process's own missing claude-code credential (a run on that runner
   * succeeded this week) and two loops parked in the operator's slot gate. The
   * committed health set is turned into that shape on the way through, over
   * otherwise-well readings, and the header must draw no "down" at either
   * width. The same payload with one heartbeat gone stale must draw "1 down",
   * or the first half proves nothing.
   */
  const liveShape = (payload, { staleDriver = false } = {}) => {
    const drivers = ['host-contract', 'openshell-runtime'].map((epic_ref) => ({
      ...payload.drivers[0],
      epic_ref,
      active: true,
      liveness: 'waiting',
      waiting_reason: 'slot',
      waiting_at: payload.checked_at,
      tick_age_s: 900,
      interval_s: 60
    }));
    if (staleDriver) drivers[1] = { ...drivers[1], liveness: 'gone', waiting_reason: null };
    return {
      ...payload,
      images: payload.images.map((image) => ({ ...image, present: true })),
      runners: payload.runners.map((runner) =>
        runner.name !== 'claude-code'
          ? runner
          : {
              ...runner,
              credential_names: runner.credential_names.map((c) => ({ ...c, set: false })),
              succeeded_in_window: 3,
              credential_note: 'not visible to kew serve; the loops hold their own'
            }
      ),
      drivers
    };
  };
  for (const width of [390, WIDTH]) {
    await check(`the header counts no slot wait or serve-only credential as down at ${width}px`, async () => {
      for (const staleDriver of [false, true]) {
        const ctx = await browser.newContext({ viewport: { width, height: width === WIDTH ? HEIGHT : 844 } });
        const page = await ctx.newPage();
        try {
          await page.route('**/api/health', async (route) => {
            const response = await route.fetch();
            await route.fulfill({ response, json: liveShape(await response.json(), { staleDriver }) });
          });
          await page.goto(`${base}/health`, { waitUntil: 'load' });
          await page.locator('#drivers').waitFor({ state: 'visible', timeout: 15_000 });
          await sleep(SETTLE_MS);
          const read = await page.evaluate(() => ({
            // Only the header's "N down" readings: the More menu also carries a
            // plain "Health" page link (nav.ts) to the same href.
            down: [...document.querySelectorAll('header a[href="/health"]')]
              .map((a) => (a.textContent ?? '').replace(/\s+/g, ' ').trim())
              .filter((t) => /\bdown\b/.test(t)),
            note: document.querySelector('#runners [data-credential-note]')?.textContent?.trim() ?? '',
            drivers: document.querySelector('#drivers')?.textContent ?? ''
          }));
          if (staleDriver) {
            expect(
              read.down.length > 0 && read.down.every((t) => t === '1 down'),
              `${width}px: one stale heartbeat should read "1 down", the header read ${JSON.stringify(read.down)}`
            );
          } else {
            expect(read.down.length === 0, `${width}px: the header still reads ${JSON.stringify(read.down)}`);
            expect(read.note.includes('not visible to kew serve'), `${width}px: no credential note on Runners`);
            expect(read.drivers.includes('waiting: slot'), `${width}px: no driver reads "waiting: slot"`);
          }
        } finally {
          await page.close();
          await ctx.close();
        }
      }
    });
  }
}
