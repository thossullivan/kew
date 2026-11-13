/**
 * The shell header in every failure state (#2418 review).
 *
 * One surface of `npm run ui-check`; `scripts/ui-check.mjs` runs it (#2570).
 */
// ui-check-owns: src/lib/shell/FactoryHeader.svelte
// ui-check-owns: src/lib/shell/header-state.ts
import { SETTLE_MS } from '../ui-check-lib.mjs';

export async function checks({ browser, base, check, expect, sleep }) {
  /**
   * #2418 review — the header above its fold, in every state at once.
   *
   * The phone check below is the one #2418 shipped, and it was not enough: the
   * fold that keeps the 390px row whole only applies BELOW `lg`, so from 1024px
   * up the row carried the full cluster with nothing on it able to give, and a
   * stale build beside a failing health check beside a stopped fleet scrolled
   * the page sideways at 1024 AND at 1280 — a width DESIGN.md forbids it at.
   * No vitest run can see that: it is layout.
   *
   * Two readings, because the first fix for it passed one and failed the other.
   * The page must not scroll, and nothing in the header may be painted OVER the
   * reading next door — a box squeezed past its own unshrinkable content does
   * the second without doing the first, so `cancel` sat on top of the repo with
   * `scrollWidth` perfectly happy.
   */
  await check('the header does not overflow or overlap itself in any failure state', async () => {
    // The loudest state the header can be in: an old build of itself, something
    // required down, the fleet stopped, a repo slug with no end to it, and a
    // refused resume with both confirm presses up beside the refusal.
    const LONG_REPO = 'some-long-github-organisation/a-rather-long-repository-name';
    const REFUSAL =
      'Refused: the resume verb needs an approver session and this one is ' +
      'read-only, so nothing was written.';

    for (const width of [390, 1024, 1280, 1440]) {
      const ctx = await browser.newContext({ viewport: { width, height: 844 } });
      const page = await ctx.newPage();
      try {
        // The fixtures draw a healthy fleet, so the readings are turned red on
        // the way through rather than committed that way: a fixture set with a
        // stopped fleet in it would change every artboard comparison.
        await page.route('**/api/dashboard*', async (route) => {
          const response = await route.fetch();
          const payload = await response.json();
          payload.banner = { ...payload.banner, fleet_stopped: true };
          payload.glance.pressure = {
            ...(payload.glance.pressure ?? {}),
            delivery_repo: LONG_REPO
          };
          await route.fulfill({ response, json: payload });
        });
        await page.route('**/api/bundle*', async (route) => {
          await route.fulfill({
            json: { present: true, git_sha: '1a2b3c4d5e6f', built_at: 0, stale: true }
          });
        });
        await page.route('**/api/verbs/resume*', async (route) => {
          await route.fulfill({ status: 403, json: { detail: REFUSAL } });
        });

        await page.goto(`${base}/work`, { waitUntil: 'load' });
        await page.locator('header').first().waitFor({ state: 'visible', timeout: 15_000 });
        await sleep(SETTLE_MS);

        // Press through to the refusal, then press resume again: the confirm
        // step and the server's answer are the widest the stop ever gets, and
        // they are only reachable by pressing. The menu is opened directly
        // because below `lg` the presses live in it.
        await page.evaluate(() => {
          const menu = document.querySelector('header details');
          if (menu) menu.open = true;
        });
        const press = async (label) => {
          for (const button of await page.locator('header button').all()) {
            if (((await button.textContent()) ?? '').trim() === label) {
              await button.evaluate((el) => el.click());
              return true;
            }
          }
          return false;
        };
        expect(await press('resume'), `${width}px: no resume press to make`);
        await sleep(150);
        expect(await press('confirm resume'), `${width}px: no confirm press to make`);
        await sleep(500);
        expect(await press('resume'), `${width}px: no resume press after the refusal`);
        await sleep(150);
        await page.evaluate(() => {
          const menu = document.querySelector('header details');
          if (menu) menu.open = false;
        });
        await sleep(150);

        const read = await page.evaluate(() => {
          const root = document.querySelector('header');
          const walk = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
          const inked = [];
          for (let node = walk.nextNode(); node; node = walk.nextNode()) {
            const text = (node.textContent ?? '').trim();
            if (text === '') continue;
            // The More menu is out of flow and may legitimately sit over the
            // page; what this reads is the ROW.
            if (node.parentElement?.closest('header details div') !== null) continue;
            const range = document.createRange();
            range.selectNodeContents(node);
            let box = range.getBoundingClientRect();
            // A Range over an ellipsised reading reports the WHOLE string's
            // width — `text-overflow` is a paint-time effect over a line box
            // that is still as wide as the text (the nav check below says the
            // same thing the other way round). So the ink is clipped by every
            // ancestor that clips it, which is what is actually on screen.
            for (let el = node.parentElement; el; el = el.parentElement) {
              if (getComputedStyle(el).overflowX === 'visible') continue;
              const clip = el.getBoundingClientRect();
              const left = Math.max(box.x, clip.x);
              const right = Math.min(box.x + box.width, clip.x + clip.width);
              box = { x: left, y: box.y, width: Math.max(0, right - left), height: box.height };
            }
            if (box.width === 0 || box.height === 0) continue;
            inked.push({ text, x: box.x, y: box.y, w: box.width, h: box.height });
          }
          return {
            scroll: document.documentElement.scrollWidth,
            client: document.documentElement.clientWidth,
            height: root.getBoundingClientRect().height,
            right: Math.max(
              ...[...root.querySelectorAll('header > *, header > * > *')].map(
                (el) => el.getBoundingClientRect().right
              )
            ),
            inked
          };
        });

        expect(
          read.scroll <= read.client,
          `the page scrolls sideways at ${width}px with everything red: ` +
            `scrollWidth ${read.scroll} over clientWidth ${read.client}`
        );
        expect(
          read.right <= read.client + 0.5,
          `the header paints past the viewport at ${width}px: its rightmost box ` +
            `ends at ${read.right.toFixed(1)} in a ${read.client}px page`
        );
        expect(
          read.height <= 56.5,
          `the header is ${read.height.toFixed(1)}px tall at ${width}px — a reading ` +
            'wrapped, and the row is 56px by design'
        );
        expect(read.inked.length > 3, `the header inked ${read.inked.length} readings at ${width}px`);
        // Half a pixel of tolerance: readings that touch by construction are
        // not readings on top of each other.
        const over = (a, b) =>
          a.x < b.x + b.w - 0.5 &&
          b.x < a.x + a.w - 0.5 &&
          a.y < b.y + b.h - 0.5 &&
          b.y < a.y + a.h - 0.5;
        for (let i = 0; i < read.inked.length; i += 1) {
          for (let j = i + 1; j < read.inked.length; j += 1) {
            expect(
              !over(read.inked[i], read.inked[j]),
              `"${read.inked[i].text}" is painted over "${read.inked[j].text}" at ${width}px`
            );
          }
        }
      } finally {
        await page.close();
        await ctx.close();
      }
    }
  });
}
