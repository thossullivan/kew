/**
 * A running item is working, never needs-you; an unknown count draws no
 * diamond (#2842).
 *
 * One surface of `npm run ui-check`; `scripts/ui-check.mjs` runs it (#2570).
 *
 * Design review c5 found #2814 wearing the needs-you diamond beside WORKING
 * while a conflict-resolve run worked on it, and the fixture listing #2055 on
 * Monitor as a working review run while it was also the merge needs-you. And
 * the Epics list's "no epic" row drew a magenta diamond beside `?` when a
 * bucket source failed — a diamond over a set nobody could read. Each check
 * reads what the BROWSER drew, at both widths.
 *
 * #2814 itself was the operator gate's finding: no run executed on it, but
 * its view said `review_round_due` ("kew re-reviews next", WORKING) while a
 * `base_integration_conflict` hold stopped that re-review. The
 * `dashboard-work-held-round-due` scenario composes SFD-229 the same way under
 * the committed set's go-live hold.
 */
// ui-check-owns: src/routes/(factory)/work/+page.svelte
// ui-check-owns: src/lib/monitor/Holds.svelte
// ui-check-owns: src/lib/monitor/LiveRuns.svelte
// ui-check-owns: src/routes/(factory)/epics/+page.svelte
// ui-check-owns: fixtures/scenarios/dashboard-work-held-round-due.json
import { readFileSync } from 'node:fs';
import { FRONTEND } from '../ui-check-lib.mjs';

const HELD_ROUND_DUE = readFileSync(
  new URL('fixtures/scenarios/dashboard-work-held-round-due.json', FRONTEND),
  'utf8'
);

/** Serve `/api/dashboard/work` as the held-between-rounds scenario. */
async function routeHeldRoundDue(page) {
  await page.route(
    (url) => url.pathname === '/api/dashboard/work',
    (route) => route.fulfill({ status: 200, contentType: 'application/json', body: HELD_ROUND_DUE })
  );
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

/** The payload with one epic-bucket source unread, as a failed reader serves it. */
function bucketSourceFailed(payload) {
  return { ...payload, unavailable: [...(payload.unavailable ?? []), 'node_epics'] };
}

export async function checks({ browser, base, check, expect, sleep, sizes: { WIDTH, HEIGHT } }) {
  const sizes = [
    [WIDTH, HEIGHT],
    [390, 844]
  ];

  /** One fresh page at *width*, its routes installed before the first load. */
  async function withPage(width, height, routes, fn) {
    const ctx = await browser.newContext({ viewport: { width, height } });
    const page = await ctx.newPage();
    try {
      for (const install of routes) await install(page);
      return await fn(page);
    } finally {
      await page.close();
      await ctx.close();
    }
  }

  for (const [width, height] of sizes) {
    await check(`no Work row wears the needs-you diamond beside WORKING at ${width}px`, () =>
      withPage(width, height, [], async (page) => {
        await page.goto(`${base}/work`, { waitUntil: 'load' });
        await page.locator('[data-item]').first().waitFor({ state: 'attached', timeout: 15_000 });
        await page
          .locator('[data-needs-you-diamond]')
          .first()
          .waitFor({ state: 'attached', timeout: 15_000 });
        await sleep(700);
        const rows = await page.evaluate(() =>
          [...document.querySelectorAll('a[data-item]')].map((row) => ({
            item: row.getAttribute('data-item'),
            diamond: row.querySelector('[data-needs-you-diamond]') !== null,
            state: (row.lastElementChild?.textContent ?? '').trim().toLowerCase()
          }))
        );
        // Both halves must be on screen, or "never together" proves nothing.
        expect(rows.some((r) => r.diamond), `${width}px: no Work row wears the diamond`);
        expect(
          rows.some((r) => /\bworking\b/.test(r.state)),
          `${width}px: no Work row reads WORKING`
        );
        const both = rows.filter((r) => r.diamond && /\bworking\b/.test(r.state));
        expect(
          both.length === 0,
          `${width}px: ${both.map((r) => r.item).join(', ')} wear(s) the diamond beside WORKING`
        );
      })
    );

    await check(`a held item due a review round reads HELD with the diamond on Work at ${width}px`, () =>
      withPage(width, height, [routeHeldRoundDue], async (page) => {
        await page.goto(`${base}/work`, { waitUntil: 'load' });
        const held = page.locator('a[data-item="SFD-229"]');
        await held.first().waitFor({ state: 'attached', timeout: 15_000 });
        await sleep(700);
        const read = await held.first().evaluate((row) => ({
          diamond: row.querySelector('[data-needs-you-diamond]') !== null,
          state: (row.lastElementChild?.textContent ?? '').trim().toLowerCase()
        }));
        expect(read.diamond, `${width}px: SFD-229 is held but wears no needs-you diamond`);
        expect(/\bheld\b/.test(read.state), `${width}px: SFD-229 reads "${read.state}", not HELD`);
        expect(
          !/\bworking\b/.test(read.state),
          `${width}px: SFD-229 is held but reads WORKING ("${read.state}")`
        );
      })
    );

    await check(`no Monitor live run is also in Holds and blocks at ${width}px`, () =>
      withPage(width, height, [], async (page) => {
        await page.goto(`${base}/monitor`, { waitUntil: 'load' });
        const live = page.locator('section[aria-label="Live runs"]');
        const holds = page.locator('section[aria-label="Holds and blocks"]');
        await live.locator('[data-live-run]').first().waitFor({ state: 'attached', timeout: 15_000 });
        await holds.locator('[data-hold-item]').first().waitFor({ state: 'attached', timeout: 15_000 });
        await sleep(700);
        const items = (section, attr) =>
          section.evaluate(
            (el, name) => [...el.querySelectorAll(`[${name}]`)].map((row) => row.getAttribute(name)),
            attr
          );
        const workingItems = new Set(await items(live, 'data-live-run'));
        const waitingItems = new Set(await items(holds, 'data-hold-item'));
        expect(workingItems.size > 0, `${width}px: Monitor lists no live run`);
        expect(waitingItems.size > 0, `${width}px: Monitor's holds name no item`);
        const both = [...workingItems].filter((item) => waitingItems.has(item));
        expect(
          both.length === 0,
          `${width}px: ${both.join(', ')} is both a live run and a needs-you hold on Monitor`
        );
      })
    );

    await check(`the "no epic" row reads a bare ? with no diamond when a bucket source fails at ${width}px`, () =>
      withPage(width, height, [(page) => routeDashboard(page, bucketSourceFailed)], async (page) => {
        await page.goto(`${base}/epics`, { waitUntil: 'load' });
        const cell = page.locator('[data-no-epic-row] [data-no-epic-needs-you]');
        await cell.first().waitFor({ state: 'attached', timeout: 15_000 });
        await sleep(700);
        const read = await page.locator('[data-no-epic-row] a').first().evaluate((a) => ({
          label: (a.querySelector('[data-no-epic-needs-you]')?.textContent ?? '').trim(),
          marks: a.querySelectorAll('svg').length,
          colour: getComputedStyle(a).color
        }));
        expect(read.label === '?', `${width}px: the "no epic" row reads "${read.label}", not "?"`);
        expect(read.marks === 0, `${width}px: the "no epic" row draws ${read.marks} diamond(s) over "?"`);
        const probe = await page.evaluate((value) => {
          const el = document.createElement('span');
          el.style.color = value;
          document.body.appendChild(el);
          const colour = getComputedStyle(el).color;
          el.remove();
          return colour;
        }, 'var(--text-3)');
        expect(
          read.colour === probe,
          `${width}px: the "no epic" "?" is drawn in ${read.colour}, not text-3 (${probe})`
        );
      })
    );
  }
}
